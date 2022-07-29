import math
from turtle import forward
import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.nn import Parameter
from torch.distributions.normal import Normal

import torch_geometric.nn as gnn


class GCNConv(gnn.conv.GCNConv):
    def __init__(
        self,
        in_channels,
        out_channels,
        improved=False,
        cached=False,
        bias=True,
        normalize=True,
        reparam_mode=None,
        prior_mode="Gaussian",
        sample_size=1,
        val_use_mean=True,
        **kwargs
    ):
        """Graph Convolution layer with GIB principle

        Args:
            in_channels (int): number of input channels
            out_channels (int): number of output channels

            reparam_mode (string, optional): reparametrization mode for latent space. Defaults to None == diagonal.
            prior_mode (string, optional): feature prior. Defaults to Gaussian.
            struct_dropout_mode (List[string], optional): structural dropout: first item should be sampling mode and second distribution. Defaults to None.
            sample_size (int, optional): sample size of latent space. Defaults to 1.
            val_use_mean (bool, optional): use latent space mean as layer';s output. Defaults to True.
            bias (bool, optional): _description_. Defaults to True.
        """

        super(GCNConv, self).__init__(
            in_channels=in_channels,
            out_channels=out_channels,
            aggr="add",
            normalize=False,
            **kwargs
        )

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.improved = improved
        self.cached = cached
        self.normalize = normalize

        self.reparam_mode = reparam_mode
        self.prior_mode = prior_mode
        self.sample_size = sample_size
        self.val_use_mean = False

        self.weight = Parameter(torch.Tensor(in_channels, out_channels))

        if bias:
            self.bias = Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter("bias", None)

        self.reset_parameters()

    def reparameterize(self, encoder_out, size=None):
        # mode = diag
        mean_logit = encoder_out
        if isinstance(mean_logit, tuple):
            mean_logit = mean_logit[0]

        size = math.ceil(mean_logit.size(-1) / 2)
        mean = mean_logit[:, :size]
        std = F.softplus(mean_logit[:, -size:], beta=1) + 1e-10
        dist = Normal(mean, std)
        return dist, (mean, std)

    def forward(self, x, edge_index, edge_weight=None):
        out = super().forward(x, edge_index, edge_weight)
        # Reparameterize:
        self.dist, _ = self.reparameterize(
            encoder_out=out, size=self.out_channels
        )  # [B, Z]

        Z = self.dist.rsample((self.sample_size,))[0]  # [S, B, Z]

        if self.prior_mode == "Gaussian":
            self.feature_prior = Normal(
                loc=torch.zeros(Z.shape).to(x.device),
                scale=torch.ones(Z.shape).to(x.device),
            )  # [B, Z]

        # Calculate prior loss:
        if self.reparam_mode == "diag" and self.prior_mode == "Gaussian":
            ixz = torch.distributions.kl.kl_divergence(
                self.dist, self.feature_prior
            ).sum(-1)
        else:
            Z_logit = (
                self.dist.log_prob(Z).sum(-1)
                # if self.reparam_mode.startswith("diag")
                # else self.dist.log_prob(Z)
            )  # [S, B]
            prior_logit = self.feature_prior.log_prob(Z).sum(-1)  # [S, B]
            # upper bound of I(X; Z):
            ixz = (Z_logit - prior_logit).mean(0)  # [B]

        self.Z_std = Z.std((0, 1))
        self.Z_std = self.Z_std.cpu().data.mean()
        # if self.val_use_mean is False or self.training:
        #     out = Z.mean(0)  # [B, Z]
        # else:
        #     out = out[:, : self.out_channels]  # [B, Z]

        structure_kl_loss = torch.zeros([]).to(x.device)
        return out, ixz, structure_kl_loss

    # def message(self, x_j, norm):
    #     return norm.view(-1, 1) * x_j if norm is not None else x_j

    def update(self, aggr_out):
        if self.bias is not None:
            aggr_out = aggr_out + self.bias
        return aggr_out

    def __repr__(self):
        return "{}({}, {})".format(
            self.__class__.__name__, self.in_channels, self.out_channels
        )


class GIBGCN(nn.Module):
    def __init__(
        self,
        num_features,
        num_classes,
        latent_size,
        reparam_mode=None,
        prior_mode=None,
        sample_size=1,
        struct_dropout_mode=("standard", 0.6),
        dropout=True,
        with_relu=True,
        val_use_mean=True,
        reparam_all_layers=True,
        normalize=True,
    ):
        super(GIBGCN, self).__init__()
        self.num_features = num_features
        self.num_classes = num_classes
        self.reparam_mode = reparam_mode
        self.prior_mode = prior_mode
        self.latent_size = latent_size
        self.sample_size = sample_size
        self.struct_dropout_mode = struct_dropout_mode
        self.dropout = dropout
        self.with_relu = with_relu
        self.val_use_mean = val_use_mean
        self.reparam_all_layers = reparam_all_layers
        self.normalize = normalize

        self.reparam_layers = []
        self.conv1 = GCNConv(
            in_channels=self.num_features,
            out_channels=self.latent_size,
            reparam_mode=self.reparam_mode,
            sample_size=self.sample_size,
            val_use_mean=self.val_use_mean,
            normalize=self.normalize,
        )
        self.conv2 = GCNConv(
            in_channels=self.latent_size,
            out_channels=self.num_classes,
            reparam_mode=self.reparam_mode,
            sample_size=self.sample_size,
            val_use_mean=self.val_use_mean,
            normalize=self.normalize,
        )

    def forward(self, data, save_latent=False):
        out_dict = {"latent_out": [], "ixz_list": [], "structure_kl_list": []}

        x, edge_index, edge_weight = data.x, data.edge_index, data.edge_attr

        # if self.use_relu? x = F.relu(x)
        # if self.dropout: x = F.dropout(x, training=self.training)

        x, ixz, structure_kl_loss = self.conv1(x, edge_index, edge_weight)
        out_dict["latent_out"] = out_dict["latent_out"] + [x]
        out_dict["ixz_list"] = out_dict["ixz_list"] + [ixz]
        out_dict["structure_kl_list"] = out_dict["structure_kl_list"] + [
            structure_kl_loss
        ]
        # save latent ==> torch.save(z)?

        x, ixz, structure_kl_loss = self.conv2(x, edge_index, edge_weight)
        out_dict["latent_out"] = out_dict["latent_out"] + [x]
        out_dict["ixz_list"] = out_dict["ixz_list"] + [ixz]
        out_dict["structure_kl_list"] = out_dict["structure_kl_list"] + [
            structure_kl_loss
        ]

        return x, out_dict
