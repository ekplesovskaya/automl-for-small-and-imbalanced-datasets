import random
from sklearn.model_selection import StratifiedKFold, StratifiedShuffleSplit
from sklearn.preprocessing import StandardScaler
from imblearn.pipeline import Pipeline
from imblearn.over_sampling import SMOTE, RandomOverSampler, ADASYN
from imblearn.under_sampling import RandomUnderSampler
import xgboost as xgb
from catboost import CatBoostClassifier
from sklearn.ensemble import RandomForestClassifier
import lightgbm as lgb
from hyperopt import fmin, tpe, space_eval
import pickle
import numpy as np
from sklearn.metrics import f1_score, accuracy_score, roc_auc_score, log_loss
from .abb import AutoBalanceBoost
from hyperopt import hp
from scipy.stats import rankdata
from datetime import datetime
import os
from copy import deepcopy
from numpy import ndarray
from typing import Union, Tuple

balance_dict = {"SMOTE": SMOTE(random_state=42),
                "RandomOverSampler": RandomOverSampler(random_state=42),
                "ADASYN": ADASYN(random_state=42, n_jobs=-1),
                "RandomUnderSampler": RandomUnderSampler(random_state=42)}

classificator_dict = {"XGB": xgb.XGBClassifier(seed=10, verbosity=0, use_label_encoder=False),
                      "RF": RandomForestClassifier(random_state=42, n_jobs=-1),
                      "LGBM": lgb.LGBMClassifier(random_state=42),
                      "catboost": CatBoostClassifier(random_seed=42, verbose=False)}

with open("/".join(str(os.path.realpath(__file__)).split("\\")).split("automl_imbalanced")[
              0] + 'automl_imbalanced/sampling_hyperparameters_space.pickle', 'rb') as f:
    space_dict = pickle.load(f)


def get_cv_type(split_num: int) -> str:
    """
    Defines the type of splitting iterations.

    Parameters
    ----------
    split_num : int
        The number of splitting iterations.

    Returns
    -------
    cv_type : str
        The chosen type of splitting iterations.
    """
    if split_num % 5 == 0:
        cv_type = "kfold"
    else:
        cv_type = "split"
    return cv_type


def scale_data(x_train: ndarray) -> Tuple[ndarray, object]:
    """
    Fits scaler and applies it to the train sample.

    Parameters
    ----------
    x_train : array-like of shape (n_samples, n_features)
        Training sample.

    Returns
    -------
    x_train_scaled : array-like of shape (n_samples, n_features)
        Scaled sample.

    scaler : instance
        Fitted scaler.
    """
    scaler = StandardScaler()
    scaler.fit(x_train)
    x_train_scaled = scaler.transform(x_train)
    return x_train_scaled, scaler


def get_sampl_strat_for_case(ss: float, count_class: ndarray, balance_method: str) -> Union[float, dict]:
    """
    Calculates the sampling strategy parameter.

    Parameters
    ----------
    ss : float
        Sampling strategy parameter generated by Hyperopt.

    count_class : array-like
        The sorted unique values with the number of counts.

    balance_method : str
        Balancing procedure label.

    Returns
    -------
    ss_corr : float or dict
        The adjusted sampling strategy parameter.
    """
    min_cl_arg = np.argmin(count_class[1])
    max_cl_arg = np.argmax(count_class[1])
    if len(count_class[0]) > 2:
        if balance_method == "RandomUnderSampler":
            new_dict = {}
            for i, val in enumerate(count_class[0]):
                if i == min_cl_arg:
                    new_dict[val] = count_class[1][i]
                else:
                    new_dict[val] = min(int(round(count_class[1][min_cl_arg] / ss, 0)), count_class[1][i])
        else:
            new_dict = {}
            for i, val in enumerate(count_class[0]):
                if i == max_cl_arg:
                    new_dict[val] = count_class[1][i]
                else:
                    new_dict[val] = max(int(round(count_class[1][max_cl_arg] * ss, 0)), count_class[1][i])
        ss_corr = new_dict
    else:
        ss_corr = max(count_class[1][min_cl_arg] / count_class[1][max_cl_arg], ss)
    return ss_corr


def calc_pipeline_acc(params: dict, x: ndarray, y: ndarray, bal_alg: str, alg: str, metric: str) -> float:
    """
    Evaluates the pipeline.

    Parameters
    ----------
    params : dict
        Parameters generated by Hyperopt.

    x : array-like of shape (n_samples, n_features)
        Training sample.

    y : array-like
        The target values.

    bal_alg : str
        Sampling procedure label.

    alg : str
        Ensemble classifier label.

    metric : str
        Metric that is used to evaluate the model performance.

    Returns
    -------
    score : float
        Evaluation of the model performance.
    """
    score_list = []
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    count = 0
    for train_index, test_index in skf.split(x, y):
        x_train, x_test = x[train_index], x[test_index]
        y_train, y_test = y[train_index], y[test_index]
        estimator = Pipeline([
            ('balancing', deepcopy(balance_dict[bal_alg])),
            ('classification', deepcopy(classificator_dict[alg]))])
        if count == 0:
            count_class = np.unique(y_train, return_counts=True)
            params["balancing__sampling_strategy"] = get_sampl_strat_for_case(params["balancing__sampling_strategy"],
                                                                              count_class, bal_alg)
            if "balancing__k_neighbors" in params:
                params["balancing__k_neighbors"] = int(params["balancing__k_neighbors"])
            elif "balancing__n_neighbors" in params:
                params["balancing__n_neighbors"] = int(params["balancing__n_neighbors"])
        estimator.set_params(**params)
        x_train_scaled, scaler = scale_data(x_train)
        try:
            estimator.fit(x_train_scaled, y_train)
            x_test_scaled = scaler.transform(x_test)
            if metric in ["roc_auc", "log_loss"]:
                pred = estimator.predict_proba(x_test_scaled)
                score_list.append(calc_metric(y_test, pred, metric))
            else:
                pred = estimator.predict(x_test_scaled)
                score_list.append(calc_metric(y_test, pred, metric))
        except:
            if metric == "log_loss":
                score_list.append(np.inf)
            else:
                score_list.append(0)
        count += 1
    score = np.mean(score_list)
    if metric != "log_loss":
        score = -score
    return score


def get_balance_params(x: ndarray, y: ndarray, bal_alg: str, alg: str, hyp_time: int, metric: str) -> dict:
    """
    Searches for optimal hyper-parameters for balancing procedure using Hyperopt.

    Parameters
    ----------
    x : array-like of shape (n_samples, n_features)
        Training sample.

    y : array-like
        The target values.

    bal_alg : str
        Sampling procedure label.

    alg : str
        Ensemble classifier label.

    hyp_time : int
        The runtime setting (in seconds) for Hyperopt optimization.

    metric : str
        Metric that is used to evaluate the model performance.

    Returns
    -------
    best : dict
        Optimal hyper-parameters for balancing procedure chosen by Hyperopt.
    """
    count_class = np.unique(y, return_counts=True)
    space_ds = space_dict[bal_alg].copy()
    min_cl_arg = np.argmin(count_class[1])
    min_class = count_class[1][min_cl_arg] / np.max(count_class[1])
    space_ds["balancing__sampling_strategy"] = hp.uniform("balancing__sampling_strategy", min_class, 1)
    best = fmin(fn=lambda params: calc_pipeline_acc(params, x, y, bal_alg, alg, metric), space=space_ds,
                algo=tpe.suggest, timeout=hyp_time, rstate=np.random.seed(42))
    best = space_eval(space_ds, best)
    if "balancing__k_neighbors" in best:
        best["balancing__k_neighbors"] = int(best["balancing__k_neighbors"])
    elif "balancing__n_neighbors" in best:
        best["balancing__n_neighbors"] = int(best["balancing__n_neighbors"])
    return best


def balance_exp(x: ndarray, y: ndarray, skf: object, bal_alg: str, alg: str, hyperopt_time: int, metric: str) -> \
        Tuple[list, list]:
    """
    Evaluates model performance on a range of splits.

    Parameters
    ----------
    x : array-like of shape (n_samples, n_features)
        Training sample.

    y : array-like
        The target values.

    skf : instance
        Splitting strategy instance.

    bal_alg : str
        Sampling procedure label.

    alg : str
        Ensemble classifier label.

    hyperopt_time : int
        The runtime setting (in seconds) for Hyperopt optimization.

    metric : str
        Metric that is used to evaluate the model performance.

    Returns
    -------
    score_list : list
        Model performance on a range of splits.

    time_list : list
         Model fitting and prediction time on a range of splits.
    """
    score_list = []
    time_list = []
    for train_index, test_index in skf.split(x, y):
        time_dict = {}
        x_train, x_test = x[train_index], x[test_index]
        y_train, y_test = y[train_index], y[test_index]
        if hyperopt_time != 0:
            balance_params = get_balance_params(x_train, y_train, bal_alg, alg, hyperopt_time, metric)
            balance_params["balancing__sampling_strategy"] = get_sampl_strat_for_case(
                balance_params["balancing__sampling_strategy"], np.unique(y_train, return_counts=True), bal_alg)
        else:
            balance_params = None
        x_train_scaled, scaler = scale_data(x_train)
        estimator = Pipeline([
            ('balancing', deepcopy(balance_dict[bal_alg])),
            ('classification', deepcopy(classificator_dict[alg]))])
        if balance_params:
            estimator.set_params(**balance_params)
        t0 = datetime.now()
        estimator.fit(x_train_scaled, y_train)
        time_dict["train_time"] = datetime.now() - t0
        x_test_scaled = scaler.transform(x_test)
        if metric in ["roc_auc", "log_loss"]:
            t0 = datetime.now()
            pred = estimator.predict_proba(x_test_scaled)
            time_dict["predict_time"] = datetime.now() - t0
            score_list.append(calc_metric(y_test, pred, metric))
        else:
            t0 = datetime.now()
            pred = estimator.predict(x_test_scaled)
            time_dict["predict_time"] = datetime.now() - t0
            score_list.append(calc_metric(y_test, pred, metric))
        time_list.append(time_dict)
    return score_list, time_list


def calc_metric(y_test: ndarray, pred: ndarray, metric: str) -> float:
    """
    Calculates the evaluation metric.

    Parameters
    ----------
    y_test : array-like
        Correct target values.

    pred : array-like
        Predicted target values.

    metric : str
        Metric that is used to evaluate the model performance.

    Returns
    -------
    score : float
        Metric value.
    """
    if metric.split("_")[0] == "f1":
        if metric == "f1_macro":
            score = f1_score(y_test, pred, average="macro")
        elif metric == "f1_micro":
            score = f1_score(y_test, pred, average="micro")
        elif metric == "f1_weighted":
            score = f1_score(y_test, pred, average="weighted")
    elif metric == "accuracy":
        score = accuracy_score(y_test, pred)
    elif metric == "roc_auc":
        if len(np.unique(y_test)) == 2:
            score = roc_auc_score(y_test, pred[:, 1])
        else:
            score = roc_auc_score(y_test, pred)
    elif metric == "log_loss":
        score = log_loss(y_test, pred)
    return score


def abb_exp(x: ndarray, y: ndarray, skf: object, metric: str) -> Tuple[list, list]:
    """
    Evaluates AutoBalanceBoost performance on a partial range of splits.

    Parameters
    ----------
    x : array-like of shape (n_samples, n_features)
        Training sample.

    y : array-like
        The target values.

    skf : instance
        Splitting strategy instance.

    metric : str
        Metric that is used to evaluate the model performance.

    Returns
    -------
    score_list : list
        Model performance on a range of splits.

    time_list : list
         Model fitting and prediction time on a range of splits.
    """
    score_list = []
    time_list = []
    for train_index, test_index in skf.split(x, y):
        x_train, x_test = x[train_index], x[test_index]
        y_train, y_test = y[train_index], y[test_index]
        time_dict = {}
        model = AutoBalanceBoost()
        t0 = datetime.now()
        model.fit(x_train, y_train)
        time_dict["train_time"] = datetime.now() - t0
        if metric in ["roc_auc", "log_loss"]:
            t0 = datetime.now()
            pred = model.predict_proba(x_test)
            time_dict["predict_time"] = datetime.now() - t0
            score_list.append(calc_metric(y_test, pred, metric))
        else:
            t0 = datetime.now()
            pred = model.predict(x_test)
            time_dict["predict_time"] = datetime.now() - t0
            score_list.append(calc_metric(y_test, pred, metric))
        time_list.append(time_dict)
    return score_list, time_list


def fit_alg(cv_type: str, x: ndarray, y: ndarray, bal_alg: Union[str, None], alg: str, hyperopt_time: int, 
            split_num: int, metric: str) -> Tuple[list, list]:
    """
    Evaluates model performance on a full range of splits.

    Parameters
    ----------
    cv_type : str
        The chosen type of splitting iterations.

    x : array-like of shape (n_samples, n_features)
        Training sample.

    y : array-like
        The target values.

    bal_alg : str or None
        Sampling procedure label.

    alg : str
        Ensemble classifier label.

    hyperopt_time : int
        The runtime setting (in seconds) for Hyperopt optimization.

    split_num : int
        The number of splitting iterations.

    metric : str
        Metric that is used to evaluate the model performance.

    Returns
    -------
    score_list : list
        Model performance on a range of splits.

    time_list : list
        Model fitting and prediction time on a range of splits.
    """
    score_list = []
    time_list = []
    if cv_type == "split":
        skf = StratifiedShuffleSplit(n_splits=split_num, test_size=0.2, random_state=42)
        if alg == "AutoBalanceBoost":
            sub_score_list, sub_time_list = abb_exp(x, y, skf, metric)
        else:
            sub_score_list, sub_time_list = balance_exp(x, y, skf, bal_alg, alg, hyperopt_time, metric)
        score_list.extend(sub_score_list)
        time_list.extend(sub_time_list)
    else:
        random.seed(42)
        seed_val = random.sample(list(range(100000)), split_num // 5)
        for seed in seed_val:
            skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=seed)
            if alg == "AutoBalanceBoost":
                sub_score_list, sub_time_list = abb_exp(x, y, skf, metric)
            else:
                sub_score_list, sub_time_list = balance_exp(x, y, skf, bal_alg, alg, hyperopt_time, metric)
            score_list.extend(sub_score_list)
            time_list.extend(sub_time_list)
    return score_list, time_list


def fit_res_model(option_label: str, x: ndarray, y: ndarray, hyp_time: int, metric: str) -> Tuple[object, object]:
    """
    Fits the resulting estimator.

    Parameters
    ----------
    option_label : str
        Classifier label.

    x : array-like of shape (n_samples, n_features)
        Training sample.

    y : array-like
        The target values.

    hyp_time : int
        The runtime setting (in seconds) for Hyperopt optimization.

    metric : str
        Metric that is used to evaluate the model performance.

    Returns
    -------
    model : instance
        Fitted estimator.

    scaler : instance
        Fitted scaler.
    """
    if option_label == "AutoBalanceBoost":
        model = AutoBalanceBoost()
        model.fit(x, y)
        scaler = None
    else:
        ol = option_label.split("+")
        bal_alg = ol[0]
        alg = ol[1]
        if hyp_time != 0:
            balance_params = get_balance_params(x, y, bal_alg, alg, hyp_time, metric)
        else:
            balance_params = None
        x_scaled, scaler = scale_data(x)
        model = Pipeline([
            ('balancing', deepcopy(balance_dict[bal_alg])),
            ('classification', deepcopy(classificator_dict[alg]))])
        if balance_params:
            balance_params["balancing__sampling_strategy"] = get_sampl_strat_for_case(
                balance_params["balancing__sampling_strategy"], np.unique(y, return_counts=True), bal_alg)
            model.set_params(**balance_params)
        model.fit(x_scaled, y)
    return model, scaler


def choose_and_fit_ilc(self, x: ndarray, y: ndarray) -> Tuple[object, str, float, object, dict, dict, tuple]:
    """
    Chooses the optimal classifier and fits the resulting estimator.

    Parameters
    ----------
    x : array-like of shape (n_samples, n_features)
        Training sample.

    y : array-like
        The target values.

    Returns
    -------
    classifer : instance
        Optimal fitted classifier.

    option_label : str
        Optimal classifier label.

    score : float
        Averaged out-of-fold value of eval_metric for the optimal classifier.

    scaler : instance
        Fitted scaler that is applied prior to classifier estimation.

    score_dict : dict
        Score series for the range of estimated classifiers.

    time_dict : dict
        Time data for the range of estimated classifiers.

    conf_int : tuple
        95% confidence interval for the out-of-fold value of eval_metric for the optimal classifier.
    """
    res_dict = {}
    score_dict = {}
    time_dict = {}
    cv_type = get_cv_type(self.split_num)
    option_list = []
    for alg in ["catboost", "RF", "LGBM", "XGB"]:
        for bal_alg in ["RandomOverSampler", "SMOTE", "RandomUnderSampler", "ADASYN"]:
            score_list, time_list = fit_alg(cv_type, x, y, bal_alg, alg, self.hyperopt_time, self.split_num,
                                            self.eval_metric)
            option_list.append(bal_alg + "+" + alg)
            score_dict[option_list[-1]] = score_list
            time_dict[option_list[-1]] = time_list
            res_dict[option_list[-1]] = np.mean(score_list)
    score_list, time_list = fit_alg(cv_type, x, y, None, "AutoBalanceBoost", self.hyperopt_time, self.split_num,
                                    self.eval_metric)
    option_list.append("AutoBalanceBoost")
    score_dict[option_list[-1]] = score_list
    time_dict[option_list[-1]] = time_list
    res_dict[option_list[-1]] = np.mean(score_list)
    if self.eval_metric == "log_loss":
        res_dict = {k: v for k, v in sorted(res_dict.items(), key=lambda item: (item[1]))}
    else:
        res_dict = {k: v for k, v in sorted(res_dict.items(), key=lambda item: (item[1]), reverse=True)}
    option_label = list(res_dict.keys())[0]
    score = list(res_dict.values())[0]
    classifier, scaler = fit_res_model(option_label, x, y, self.hyperopt_time, self.eval_metric)
    conf_int = (np.percentile(score_dict[option_label], 2.5), np.percentile(score_dict[option_label], 97.5))
    return classifier, option_label, score, scaler, score_dict, time_dict, conf_int


def calc_leaderboard(self) -> dict:
    """
    Calculates the leaderboard statistics.

    Returns
    -------
    ls : dict
        The leaderboard statistics that includes sorted lists in accordance with the following indicators:
        "Mean score", "Mean rank", "Share of experiments with the first place, %",
        "Average difference with the leader, %".
    """
    ls = {}
    mean_dict = {}
    sub_rank_list = [[] for el in list(range(self.split_num))]
    for el in list(self.evaluated_models_scores_.keys()):
        mean_dict[el] = np.mean(self.evaluated_models_scores_[el])
        for i, val in enumerate(self.evaluated_models_scores_[el]):
            if self.eval_metric == "log_loss":
                sub_rank_list[i].append(val)
            else:
                sub_rank_list[i].append(-val)
    if self.eval_metric == "log_loss":
        mean_dict = {k: v for k, v in sorted(mean_dict.items(), key=lambda item: (item[1]))}
    else:
        mean_dict = {k: v for k, v in sorted(mean_dict.items(), key=lambda item: (item[1]), reverse=True)}
    leader = list(mean_dict.keys())[0]
    ls["Mean score"] = mean_dict
    model_rank = []
    for i in range(len(sub_rank_list)):
        model_rank.append(rankdata(sub_rank_list[i], method='dense'))
    model_rank = np.array(model_rank)
    rank_dict = {}
    leader_share = {}
    diff_dict = {}
    for i, el in enumerate(list(self.evaluated_models_scores_.keys())):
        rank_dict[el] = np.mean(model_rank[:, i])
        leader_share[el] = model_rank[model_rank[:, i] == 1, i].shape[0] / model_rank.shape[0] * 100
        if el != leader:
            diff = (np.array(self.evaluated_models_scores_[el]) - np.array(
                self.evaluated_models_scores_[leader])) / np.array(self.evaluated_models_scores_[leader]) * 100
            diff_dict[el] = np.mean(diff)
    rank_dict = {k: v for k, v in sorted(rank_dict.items(), key=lambda item: (item[1]))}
    leader_share = {k: v for k, v in sorted(leader_share.items(), key=lambda item: (item[1]), reverse=True)}
    diff_dict = {k: v for k, v in sorted(diff_dict.items(), key=lambda item: (item[1]), reverse=True)}
    ls["Mean rank"] = rank_dict
    ls["Share of experiments with the first place, %"] = leader_share
    ls["Average difference with the leader, %"] = diff_dict
    return ls
