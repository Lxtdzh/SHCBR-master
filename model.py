import numpy as np
import scipy.sparse as sp
import torch
import torch.nn as nn
from torch_sparse import SparseTensor

def normalize_HyperGraph(H):
    # Degree of edges
    D_v = sp.diags(1 / (np.sqrt(H.sum(axis=1).A.ravel()) + 1e-8))
    # Degree of nodes
    D_e = sp.diags(1 / (np.sqrt(H.sum(axis=0).A.ravel()) + 1e-8))
    # Regularised structured hypergraph convolutional networks
    H_nomalized = D_v @ H @ D_e @ H.T @ D_v
    return H_nomalized

def Split_HyperGraph(H, device, split_num=16):
    H_list = []
    length = H.shape[0] // split_num
    for i in range(split_num):
        if i == split_num - 1:
            H_list.append(H[length * i: H.shape[0]])
        else:
            H_list.append(H[length * i: length * (i + 1)])
    H_split = [SparseTensor.from_scipy(H_i).to(device) for H_i in H_list]
    return H_split

def mix_Hypergraph(raw_graph, threshold=10):
    # Getting the Interaction Matrix
    ui_graph, bi_graph, ub_graph = raw_graph
    # User Similarity Matrix
    uu_graph = ub_graph @ ub_graph.T
    for i in range(ub_graph.shape[0]):
        for r in range(uu_graph.indptr[i], uu_graph.indptr[i + 1]):
            uu_graph.data[r] = 1 if uu_graph.data[r] > threshold else 0
    # Bundle Similarity Matrix
    bb_graph = ub_graph.T @ ub_graph
    for i in range(ub_graph.shape[1]):
        for r in range(bb_graph.indptr[i], bb_graph.indptr[i + 1]):
            bb_graph.data[r] = 1 if bb_graph.data[r] > threshold else 0
    # Equation (3) in the paper
    H_i = sp.vstack((ui_graph, bi_graph))
    # Equation (4) in the paper
    H_ub = sp.hstack((sp.vstack((uu_graph, ub_graph.T)), sp.vstack((ub_graph, bb_graph))))
    # Equation (5) in the paper
    H = sp.hstack((H_i, H_ub))
    return H


class SCHBR(nn.Module):
    def __init__(self, raw_graph, device, dp, l2_norm, emb_size=64):
        super().__init__()

        ui_graph, bi_graph, ub_graph = raw_graph
        self.num_users, self.num_bundles, self.num_items = (
            ub_graph.shape[0],
            ub_graph.shape[1],
            ui_graph.shape[1],
        )
        H = mix_Hypergraph(raw_graph)
        self.atom_graph = Split_HyperGraph(normalize_HyperGraph(H), device)
        # Initial Embeddings
        self.users_feature = nn.Parameter(
            torch.FloatTensor(self.num_users, emb_size).normal_(0, 0.5 / emb_size)
        )
        self.bundles_feature = nn.Parameter(
            torch.FloatTensor(self.num_bundles, emb_size).normal_(0, 0.5 / emb_size)
        )
        self.user_bound = nn.Parameter(
            torch.FloatTensor(emb_size, 1).normal_(0, 0.5 / emb_size)
        )
        self.drop = nn.Dropout(dp)
        self.embed_L2_norm = l2_norm

    def propagate(self):
        embed_0 = torch.cat([self.users_feature, self.bundles_feature], dim=0)
        embed_1 = torch.cat([G @ embed_0 for G in self.atom_graph], dim=0)
        all_embeds = embed_0 / 2 + self.drop(embed_1) / 2
        users_feature, bundles_feature = torch.split(
            all_embeds, [self.num_users, self.num_bundles], dim=0
        )

        return users_feature, bundles_feature

    def predict(self, users_feature, bundles_feature):
        pred = torch.sum(users_feature * bundles_feature, 2)
        return pred

    def regularize(self, users_feature, bundles_feature):
        loss = self.embed_L2_norm * ((users_feature ** 2).sum() + (bundles_feature ** 2).sum())
        return loss

    def forward(self, users, bundles):
        users_feature, bundles_feature = self.propagate()
        users_embedding = users_feature[users].expand(-1, bundles.shape[1], -1)
        bundles_embedding = bundles_feature[bundles]
        pred = self.predict(users_embedding, bundles_embedding)
        loss = self.regularize(users_feature, bundles_feature)
        user_score_bound = users_feature[users] @ self.user_bound
        return pred, user_score_bound, loss

    def evaluate(self, propagate_result, users):
        users_feature, bundles_feature = propagate_result
        users_feature = users_feature[users]
        scores = users_feature @ (bundles_feature.T)
        return scores
