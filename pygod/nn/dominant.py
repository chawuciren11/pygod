import math
import torch
import torch.nn as nn
from torch_geometric.nn import GCN

from .decoder import DotProductDecoder
from .functional import double_mse_loss


class DOMINANTBase(nn.Module):
    """
    DOMINANT (Deep Anomaly Detection on Attributed Networks) is an
    anomaly detector consisting of a shared graph convolutional
    encoder, a structure reconstruction decoder, and an attribute
    reconstruction decoder. The reconstruction mean square error of the
    decoders are defined as structure anomaly score and attribute
    anomaly score, respectively.

    See :cite:`ding2019deep` for details.

    Parameters
    ----------
    in_dim : int
        Input dimension of node features.
    hid_dim :  int
       Hidden dimension of model.
    num_layers : int, optional
       Total number of layers in model. A half (floor) of the layers
       are for the encoder, the other half (ceil) of the layers are
       for decoders. Default: ``4``.
    dropout : float, optional
       Dropout rate. Default: ``0.``.
    weight_decay : float, optional
       Weight decay (L2 penalty). Default: ``0.``.
    act : callable activation function or None, optional
       Activation function if not None.
       Default: ``torch.nn.functional.relu``.
    sigmoid_s : bool, optional
        Whether to apply sigmoid to the structure reconstruction.
        Default: ``False``.
    backbone : torch.nn.Module, optional
        The backbone of the deep detector implemented in PyG.
        Default: ``torch_geometric.nn.GCN``.
    **kwargs : optional
        Additional arguments for the backbone.
    """

    def __init__(self,
                 in_dim,
                 hid_dim,
                 num_layers=4,
                 dropout=0.,
                 act=torch.nn.functional.relu,
                 sigmoid_s=False,
                 backbone=GCN,
                 **kwargs):
        super(DOMINANTBase, self).__init__()

        # split the number of layers for the encoder and decoders
        assert num_layers >= 2, \
            "Number of layers must be greater than or equal to 2."
        encoder_layers = math.floor(num_layers / 2)
        decoder_layers = math.ceil(num_layers / 2)

        self.shared_encoder = backbone(in_channels=in_dim,
                                       hidden_channels=hid_dim,
                                       num_layers=encoder_layers,
                                       out_channels=hid_dim,
                                       dropout=dropout,
                                       act=act,
                                       **kwargs)

        self.attr_decoder = backbone(in_channels=hid_dim,
                                     hidden_channels=hid_dim,
                                     num_layers=decoder_layers,
                                     out_channels=in_dim,
                                     dropout=dropout,
                                     act=act,
                                     **kwargs)

        self.struct_decoder = DotProductDecoder(in_dim=hid_dim,
                                                hid_dim=hid_dim,
                                                num_layers=decoder_layers - 1,
                                                dropout=dropout,
                                                act=act,
                                                sigmoid_s=sigmoid_s,
                                                backbone=backbone,
                                                **kwargs)

        self.loss_func = double_mse_loss

    def forward(self, x, edge_index):

        # encode feature matrix
        h = self.shared_encoder(x, edge_index)

        # reconstruct feature matrix
        x_ = self.attr_decoder(h, edge_index)

        # decode adjacency matrix
        s_ = self.struct_decoder(h, edge_index)

        return x_, s_
