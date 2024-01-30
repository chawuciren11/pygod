# -*- coding: utf-8 -*-
"""Base classes for all outlier detector"""
# Author: Yue Zhao <zhaoy@cmu.edu>, Kay Liu <zliu234@uic.edu>
# License: BSD 2 clause

import time
from inspect import signature
from abc import ABC, abstractmethod

import torch
import numpy as np
from scipy.stats import binom
from scipy.special import erf

from torch_geometric.nn import GIN
from torch_geometric import compile
from torch_geometric.loader import NeighborLoader

from ..utils import logger, validate_device, pprint, is_fitted


class Detector(ABC):
    """Abstract class for all outlier detection algorithms.

    Parameters
    ----------
    contamination : float, optional
        The amount of contamination of the dataset in (0., 0.5], i.e.,
        the proportion of outliers in the dataset. Used when fitting to
        define the threshold on the decision function. Default: ``0.1``.
    verbose : int, optional
        Verbosity mode. Range in [0, 3]. Larger value for printing out
        more log information. Default: ``0``.

    Attributes
    ----------
    decision_score_ : torch.Tensor
        The outlier scores of the training data. Outliers tend to have
        higher scores. This value is available once the detector is
        fitted.

    threshold_ : float
        The threshold is based on ``contamination``. It is the
        :math:`N \\times` ``contamination`` most abnormal samples in
        ``decision_score_``. The threshold is calculated for generating
        binary outlier labels.

    label_ : torch.Tensor
        The binary labels of the training data. 0 stands for inliers
        and 1 for outliers. It is generated by applying
        ``threshold_`` on ``decision_score_``.
    """

    def __init__(self,
                 contamination=0.1,
                 verbose=0):

        if not (0. < contamination <= 0.5):
            raise ValueError("contamination must be in (0, 0.5], "
                             "got: %f" % contamination)

        self.contamination = contamination
        self.verbose = verbose
        self.decision_score_ = None

    @abstractmethod
    def process_graph(self, data):
        """
        Data preprocessing for the input graph.

        Parameters
        ----------
        data : torch_geometric.data.Data
            The input graph.
        """

    @abstractmethod
    def fit(self, data, label=None):
        """Fit detector with training data.

        Parameters
        ----------
        data : torch_geometric.data.Data
            The training graph.
        label : torch.Tensor, optional
            The optional outlier ground truth labels used to monitor
            the training progress. They are not used to optimize the
            unsupervised model. Default: ``None``.

        Returns
        -------
        self : object
            Fitted detector.
        """

    @abstractmethod
    def decision_function(self, data, label=None):
        """Predict raw outlier scores of testing data using the fitted
        detector. Outliers are assigned with higher outlier scores.

        Parameters
        ----------
        data : torch_geometric.data.Data
            The testing graph.
        label : torch.Tensor, optional
            The optional outlier ground truth labels used for testing.
            Default: ``None``.

        Returns
        -------
        score : torch.Tensor
            The outlier scores of shape :math:`N`.
        """

    def predict(self,
                data=None,
                label=None,
                return_pred=True,
                return_score=False,
                return_prob=False,
                prob_method='linear',
                return_conf=False):
        """Prediction for testing data using the fitted detector.
        Return predicted labels by default.

        Parameters
        ----------
        data : torch_geometric.data.Data, optional
            The testing graph. If ``None``, the training data is used.
            Default: ``None``.
        label : torch.Tensor, optional
            The optional outlier ground truth labels used for testing.
            Default: ``None``.
        return_pred : bool, optional
            Whether to return the predicted binary labels. The labels
            are determined by the outlier contamination on the raw
            outlier scores. Default: ``True``.
        return_score : bool, optional
            Whether to return the raw outlier scores.
            Default: ``False``.
        return_prob : bool, optional
            Whether to return the outlier probabilities.
            Default: ``False``.
        prob_method : str, optional
            The method to convert the outlier scores to probabilities.
            Two approaches are possible:

            1. ``'linear'``: simply use min-max conversion to linearly
            transform the outlier scores into the range of
            [0,1]. The model must be fitted first.

            2. ``'unify'``: use unifying scores,
            see :cite:`kriegel2011interpreting`.

            Default: ``'linear'``.
        return_conf : boolean, optional
            Whether to return the model's confidence in making the same
            prediction under slightly different training sets.
            See :cite:`perini2020quantifying`. Default: ``False``.

        Returns
        -------
        pred : torch.Tensor
            The predicted binary outlier labels of shape :math:`N`.
            0 stands for inliers and 1 for outliers.
            Only available when ``return_label=True``.
        score : torch.Tensor
            The raw outlier scores of shape :math:`N`.
            Only available when ``return_score=True``.
        prob : torch.Tensor
            The outlier probabilities of shape :math:`N`.
            Only available when ``return_prob=True``.
        conf : torch.Tensor
            The prediction confidence of shape :math:`N`.
            Only available when ``return_conf=True``.
        """

        is_fitted(self, ['decision_score_', 'threshold_', 'label_'])

        output = ()
        if data is None:
            score = self.decision_score_
            logger(score=self.decision_score_,
                   target=label,
                   verbose=self.verbose,
                   train=False)
        else:
            score = self.decision_function(data, label)
        if return_pred:
            pred = (score > self.threshold_).long()
            output += (pred,)
        if return_score:
            output += (score,)
        if return_prob:
            prob = self._predict_prob(score, prob_method)
            output += (prob,)
        if return_conf:
            conf = self._predict_conf(score)
            output += (conf,)

        if len(output) == 1:
            return output[0]
        else:
            return output

    def _predict_prob(self, score, method='linear'):
        """Predict the probabilities of being outliers. Two approaches
        are possible:

        'linear': simply use min-max conversion to linearly
                  transform the outlier scores into the range of
                  [0,1]. The model must be fitted first.

        'unify': use unifying scores,
                 see :cite:`kriegel2011interpreting`.

        Parameters
        ----------
        score : torch.Tensor
            The outlier scores of shape :math:`N`.

        method : str
            probability conversion method. It must be one of
            'linear' or 'unify'. Default: ``linear``.

        Returns
        -------
        prob : torch.Tensor
            The outlier probabilities of shape :math:`N`.
        """

        if method == 'linear':
            train_score = self.decision_score_
            prob = score - train_score.min()
            prob /= train_score.max() - train_score.min()
            prob = prob.clamp(0, 1)
        elif method == 'unify':
            mu = torch.mean(self.decision_score_)
            sigma = torch.std(self.decision_score_)
            pre_erf_score = (score - mu) / (sigma * np.sqrt(2))
            erf_score = erf(pre_erf_score)
            prob = erf_score.clamp(0, 1)
        else:
            raise ValueError(method,
                             'is not a valid probability conversion method')
        return prob

    def _predict_conf(self, score):
        """Predict the model's confidence in making the same prediction
        under slightly different training sets.
        See :cite:`perini2020quantifying`.

        Parameters
        ----------
        score : torch.Tensor
            The outlier score of shape :math:`N`.

        Returns
        -------
        conf : torch.Tensor
            The prediction confidence of shape :math:`N`.
        """

        n = len(self.decision_score_)
        k = n - int(n * self.contamination)

        n_ins = (self.decision_score_.view(n, 1) <= score).count_nonzero(dim=0)

        # Derive the outlier probability using Bayesian approach
        post_prob = (1 + n_ins) / (2 + n)

        # Transform the outlier probability into a confidence value
        conf = torch.Tensor(1 - binom.cdf(k, n, post_prob))

        pred = (score > self.threshold_).long()
        conf = torch.where(pred == 0, 1 - conf, conf)
        return conf

    def _process_decision_score(self):
        """Internal function to calculate key attributes:
        - threshold_: used to decide the binary label
        - label_: binary labels of training data
        """

        self.threshold_ = np.percentile(self.decision_score_,
                                        100 * (1 - self.contamination))
        self.label_ = (self.decision_score_ > self.threshold_).long()

    def __repr__(self):

        class_name = self.__class__.__name__
        init_signature = signature(self.__init__)
        parameters = [p for p in init_signature.parameters.values()
                      if p.name != 'self' and p.kind != p.VAR_KEYWORD]
        params = {}
        for key in sorted([p.name for p in parameters]):
            params[key] = getattr(self, key, None)
        return '%s(%s)' % (class_name, pprint(params, offset=len(class_name)))


class DeepDetector(Detector, ABC):
    """
    Abstract class for deep outlier detection algorithms.

    Parameters
    ----------
    hid_dim :  int, optional
        Hidden dimension of model. Default: ``64``.
    num_layers : int, optional
        Total number of layers in model. Default: ``2``.
    dropout : float, optional
        Dropout rate. Default: ``0.``.
    weight_decay : float, optional
        Weight decay (L2 penalty). Default: ``0.``.
    act : callable activation function or None, optional
        Activation function if not None.
        Default: ``torch.nn.functional.relu``.
    backbone : torch.nn.Module
        The backbone of the deep detector implemented in PyG.
        Default: ``torch_geometric.nn.GIN``.
    contamination : float, optional
        The amount of contamination of the dataset in (0., 0.5], i.e.,
        the proportion of outliers in the dataset. Used when fitting to
        define the threshold on the decision function. Default: ``0.1``.
    lr : float, optional
        Learning rate. Default: ``0.004``.
    epoch : int, optional
        Maximum number of training epoch. Default: ``100``.
    gpu : int
        GPU Index, -1 for using CPU. Default: ``-1``.
    batch_size : int, optional
        Minibatch size, 0 for full batch training. Default: ``0``.
    num_neigh : int, optional
        Number of neighbors in sampling, -1 for all neighbors.
        Default: ``-1``.
    gan : bool, optional
        Whether using adversarial training. Default: ``False``.
    verbose : int, optional
        Verbosity mode. Range in [0, 3]. Larger value for printing out
        more log information. Default: ``0``.
    save_emb : bool, optional
        Whether to save the embedding. Default: ``False``.
    compile_model : bool, optional
        Whether to compile the model with ``torch_geometric.compile``.
        Default: ``False``.
    **kwargs
        Other parameters for the backbone.

    Attributes
    ----------
    decision_score_ : torch.Tensor
        The outlier scores of the training data. Outliers tend to have
        higher scores. This value is available once the detector is
        fitted.
    threshold_ : float
        The threshold is based on ``contamination``. It is the
        :math:`N \\times` ``contamination`` most abnormal samples in
        ``decision_score_``. The threshold is calculated for generating
        binary outlier labels.
    label_ : torch.Tensor
        The binary labels of the training data. 0 stands for inliers
        and 1 for outliers. It is generated by applying
        ``threshold_`` on ``decision_score_``.
    emb : torch.Tensor or tuple of torch.Tensor or None
        The learned node hidden embeddings of shape
        :math:`N \\times` ``hid_dim``. Only available when ``save_emb``
        is ``True``. When the detector has not been fitted, ``emb`` is
        ``None``. When the detector has multiple embeddings,
        ``emb`` is a tuple of torch.Tensor.
    """

    def __init__(self,
                 hid_dim=64,
                 num_layers=2,
                 dropout=0.,
                 weight_decay=0.,
                 act=torch.nn.functional.relu,
                 backbone=GIN,
                 contamination=0.1,
                 lr=4e-3,
                 epoch=100,
                 gpu=-1,
                 batch_size=0,
                 num_neigh=-1,
                 verbose=0,
                 gan=False,
                 save_emb=False,
                 compile_model=False,
                 **kwargs):

        super(DeepDetector, self).__init__(contamination=contamination,
                                           verbose=verbose)

        # model param
        self.in_dim = None
        self.num_nodes = None
        self.hid_dim = hid_dim
        self.num_layers = num_layers
        self.dropout = dropout
        self.weight_decay = weight_decay
        self.act = act
        self.backbone = backbone
        self.kwargs = kwargs

        # training param
        self.lr = lr
        self.epoch = epoch
        self.device = validate_device(gpu)
        self.batch_size = batch_size
        self.gan = gan
        if type(num_neigh) is int:
            self.num_neigh = [num_neigh] * self.num_layers
        elif type(num_neigh) is list:
            if len(num_neigh) != self.num_layers:
                raise ValueError('Number of neighbors should have the '
                                 'same length as hidden layers dimension or'
                                 'the number of layers.')
            self.num_neigh = num_neigh
        else:
            raise ValueError('Number of neighbors must be int or list of int')

        # other param
        self.model = None
        self.save_emb = save_emb
        if save_emb:
            self.emb = None
        self.compile_model = compile_model

    def fit(self, data, label=None):

        self.process_graph(data)
        self.num_nodes, self.in_dim = data.x.shape
        if self.batch_size == 0:
            self.batch_size = data.x.shape[0]
        loader = NeighborLoader(data,
                                self.num_neigh,
                                batch_size=self.batch_size)

        self.model = self.init_model(**self.kwargs)
        if self.compile_model:
            self.model = compile(self.model)
        if self.gan:
            opt_g = torch.optim.Adam(self.model.generator.parameters(),
                                     lr=self.lr,
                                     weight_decay=self.weight_decay)
            opt_d = torch.optim.Adam(self.model.discriminator.parameters(),
                                     lr=self.lr,
                                     weight_decay=self.weight_decay)
        else:
            optimizer = torch.optim.Adam(self.model.parameters(),
                                         lr=self.lr,
                                         weight_decay=self.weight_decay)

        self.model.train()
        self.decision_score_ = torch.zeros(data.x.shape[0])
        for epoch in range(self.epoch):
            start_time = time.time()
            if self.gan:
                epoch_loss_g = 0
                epoch_loss_d = 0
            else:
                epoch_loss = 0
            for sampled_data in loader:
                batch_size = sampled_data.batch_size
                node_idx = sampled_data.n_id

                loss, score = self.forward_model(sampled_data)

                if self.gan:
                    epoch_loss_g += loss[0].item() * batch_size
                    epoch_loss_d += loss[1].item() * batch_size
                else:
                    epoch_loss += loss.item() * batch_size

                if self.save_emb:
                    if type(self.emb) is tuple:
                        self.emb[0][node_idx[:batch_size]] = \
                            self.model.emb[0][:batch_size].cpu()
                        self.emb[1][node_idx[:batch_size]] = \
                            self.model.emb[1][:batch_size].cpu()
                    else:
                        self.emb[node_idx[:batch_size]] = \
                            self.model.emb[:batch_size].cpu()
                self.decision_score_[node_idx[:batch_size]] = score

                if self.gan:
                    opt_g.zero_grad()
                    loss[0].backward()
                    opt_g.step()
                    opt_d.zero_grad()
                    loss[0].backward()
                    opt_d.step()
                else:
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()

            if self.gan:
                loss_value = (self.epoch_loss_g / data.x.shape[0],
                              self.epoch_loss_d / data.x.shape[0])
            else:
                loss_value = epoch_loss / data.x.shape[0]
            logger(epoch=epoch,
                   loss=loss_value,
                   score=self.decision_score_,
                   target=label,
                   time=time.time() - start_time,
                   verbose=self.verbose,
                   train=True)

        self._process_decision_score()
        return self

    def decision_function(self, data, label=None):

        self.process_graph(data)
        loader = NeighborLoader(data,
                                self.num_neigh,
                                batch_size=self.batch_size)

        self.model.eval()
        outlier_score = torch.zeros(data.x.shape[0])
        if self.save_emb:
            if type(self.hid_dim) is tuple:
                self.emb = (torch.zeros(data.x.shape[0], self.hid_dim[0]),
                            torch.zeros(data.x.shape[0], self.hid_dim[1]))
            else:
                self.emb = torch.zeros(data.x.shape[0], self.hid_dim)
        start_time = time.time()
        if self.gan:
            test_loss_g = 0
            test_loss_d = 0
        else:
            test_loss = 0
        for sampled_data in loader:
            loss, score = self.forward_model(sampled_data)
            batch_size = sampled_data.batch_size
            node_idx = sampled_data.n_id
            if self.save_emb:
                if type(self.hid_dim) is tuple:
                    self.emb[0][node_idx[:batch_size]] = \
                        self.model.emb[0][:batch_size].cpu()
                    self.emb[1][node_idx[:batch_size]] = \
                        self.model.emb[1][:batch_size].cpu()
                else:
                    self.emb[node_idx[:batch_size]] = \
                        self.model.emb[:batch_size].cpu()

            if self.gan:
                test_loss_g += loss[0].item() * batch_size
                test_loss_d = loss[1].item() * batch_size
            else:
                test_loss = loss.item() * batch_size

            outlier_score[node_idx[:batch_size]] = score

        if self.gan:
            loss_value = (test_loss_g / data.x.shape[0],
                    test_loss_d / data.x.shape[0])
        else:
            loss_value = test_loss / data.x.shape[0]

        logger(loss=loss_value,
               score=outlier_score,
               target=label,
               time=time.time() - start_time,
               verbose=self.verbose,
               train=False)
        return outlier_score

    def predict(self,
                data=None,
                label=None,
                return_pred=True,
                return_score=False,
                return_prob=False,
                prob_method='linear',
                return_conf=False,
                return_emb=False):
        """Prediction for testing data using the fitted detector.
        Return predicted labels by default.

        Parameters
        ----------
        data : torch_geometric.data.Data, optional
            The testing graph. If ``None``, the training data is used.
            Default: ``None``.
        label : torch.Tensor, optional
            The optional outlier ground truth labels used for testing.
            Default: ``None``.
        return_pred : bool, optional
            Whether to return the predicted binary labels. The labels
            are determined by the outlier contamination on the raw
            outlier scores. Default: ``True``.
        return_score : bool, optional
            Whether to return the raw outlier scores.
            Default: ``False``.
        return_prob : bool, optional
            Whether to return the outlier probabilities.
            Default: ``False``.
        prob_method : str, optional
            The method to convert the outlier scores to probabilities.
            Two approaches are possible:

            1. ``'linear'``: simply use min-max conversion to linearly
            transform the outlier scores into the range of
            [0,1]. The model must be fitted first.

            2. ``'unify'``: use unifying scores,
            see :cite:`kriegel2011interpreting`.

            Default: ``'linear'``.
        return_conf : boolean, optional
            Whether to return the model's confidence in making the same
            prediction under slightly different training sets.
            See :cite:`perini2020quantifying`. Default: ``False``.
        return_emb : bool, optional
            Whether to return the learned node representations.
            Default: ``False``.

        Returns
        -------
        pred : torch.Tensor
            The predicted binary outlier labels of shape :math:`N`.
            0 stands for inliers and 1 for outliers.
            Only available when ``return_label=True``.
        score : torch.Tensor
            The raw outlier scores of shape :math:`N`.
            Only available when ``return_score=True``.
        prob : torch.Tensor
            The outlier probabilities of shape :math:`N`.
            Only available when ``return_prob=True``.
        conf : torch.Tensor
            The prediction confidence of shape :math:`N`.
            Only available when ``return_conf=True``.
        """
        if return_emb:
            self.save_emb = True

        output = super(DeepDetector, self).predict(data,
                                                   label,
                                                   return_pred,
                                                   return_score,
                                                   return_prob,
                                                   prob_method,
                                                   return_conf)
        if return_emb:
            if type(output) is tuple:
                output += (self.emb,)
            else:
                output = (output, self.emb)

        return output

    @abstractmethod
    def init_model(self, **kwargs):
        """
        Initialize the neural network detector.

        Returns
        -------
        model : torch.nn.Module
            The initialized neural network detector.
        """

    @abstractmethod
    def forward_model(self, data):
        """
        Forward pass of the neural network detector.

        Parameters
        ----------
        data : torch_geometric.data.Data
            The input graph.

        Returns
        -------
        loss : torch.Tensor
            The loss of the current batch.
        score : torch.Tensor
            The outlier scores of the current batch.
        """
