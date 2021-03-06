# encoding: utf-8
"""
@file: proteinGCN.py
@time: 5/14/21 8:47 PM
@desc:  Forked from malllabiisc/ProteinGCN, see their research for more details
"""
from __future__ import print_function, division

import os
import json
import shutil
import torch
import torch.nn as nn
import torch.optim as optim
from utils.old_utils import randomSeed


class ConvLayer(nn.Module):
    """
    Convolutional operation on graphs
    """

    def __init__(self, h_a, h_b, random_seed=None):
        """
        Initialization

        Parameters
        ----------
        h_a: int
            Atom embedding dimension
        h_b: int
            Bond embedding dimension
        random_seed: int
            Seed to reproduce consistent runs
        """
        randomSeed(random_seed)
        super(ConvLayer, self).__init__()
        self.h_a = h_a
        self.h_b = h_b
        self.fc_full = nn.Linear(2 * self.h_a + self.h_b, 2 * self.h_a)
        self.sigmoid = nn.Sigmoid()
        self.activation_hidden = nn.ReLU()

        self.bn_hidden = nn.BatchNorm1d(2 * self.h_a)
        self.bn_output = nn.BatchNorm1d(self.h_a)
        self.activation_output = nn.ReLU()

    def forward(self, atom_emb, nbr_emb, nbr_adj_list, atom_mask):
        """
        Forward pass
        :param atom_emb:     [B, n_atom_true, h_a],               Atom hidden embeddings before convolution
        :param nbr_emb:      [B, n_atom, n_neighbor=50, 43=40+3], Bond embeddings of each atom's neighbors
        :param nbr_adj_list: [B, n_atom, n_neighbor=50],          Indices of the neighbors of each atom
        :param atom_mask:    [B, n_atom, 1],                      n_atom_true = n_atom

        :return out Atom hidden embeddings after convolution
        """
        N, n_neighb = nbr_adj_list.shape[1:]  # except batch_size
        B = atom_emb.shape[0]

        # atom_emb[1, 22470, 64], [ [[0],[1], ... B],  [B, n_atom*n_neighbor] ]
        #                         [ index_of_protein,   order of atom_emb lay ]
        # [B, N, n_neighb, self.h_a]
        atom_nbr_emb = atom_emb[torch.arange(B).unsqueeze(-1), nbr_adj_list.view(B, -1)].view(B, N, n_neighb, self.h_a)
        atom_nbr_emb *= atom_mask.unsqueeze(-1)
        # [B, n_atom, 1, h_a] copy to [B, n_atom, n_neighb, h_a] concat [B, N, n_neighb, h_a]
        # [B, n_atom, n_neighb, h_this_a : h_neighbs : h_edge]
        total_nbr_emb = torch.cat([atom_emb.unsqueeze(2).expand(B, N, n_neighb, self.h_a), atom_nbr_emb, nbr_emb], dim=-1)
        total_gated_emb = self.fc_full(total_nbr_emb)  # [B, n_atom, n_neighb, 2*h_a]
        total_gated_emb = self.bn_hidden(total_gated_emb.view(-1, self.h_a * 2)).view(B, N, n_neighb, self.h_a * 2)
        # [B, n_atom, n_neighb, h_a]
        nbr_filter, nbr_core = total_gated_emb.chunk(2, dim=3)  # divide into 2 block along with dim 3
        nbr_filter = self.sigmoid(nbr_filter)  # 0-1
        nbr_core = self.activation_hidden(nbr_core)
        # features combine from neighbors to this atom with torch.num()
        nbr_sumed = torch.sum(nbr_filter * nbr_core, dim=2)  # [B, n_atom, h_a]

        nbr_sumed = self.bn_output(nbr_sumed.view(-1, self.h_a)).view(B, N, self.h_a)
        out = self.activation_output(atom_emb + nbr_sumed)
        # [B, n_atom, h_a]
        return out


class ProteinGCN(nn.Module):
    """
    Model to predict properties from protein graph - does all the convolution to get the protein embedding
    """

    def __init__(self, **kwargs):
        super(ProteinGCN, self).__init__()

        self.build(**kwargs)

        self.criterion = nn.MSELoss()
        self.inputs = None
        self.targets = None
        self.outputs = None
        self.loss = 0
        self.accuracy = 0
        self.optimizer = None
        lr = kwargs.get('lr', 0.001)
        self.optimizer = optim.SGD(self.parameters(), lr, momentum=0.9, weight_decay=0)

    def build(self, **kwargs):
        # Get atom embeddings, atom_init_file is the one-hot atom matrix
        # print('...............', kwargs.get('pkl_dir'), kwargs.get('atom_init'))
        self.atom_init_file = os.path.join(kwargs.get('atom_init'))
        with open(self.atom_init_file) as f:
            loaded_embed = json.load(f)
        # value: [167,], embed_list: [[167,], ...167]
        embed_list = [torch.tensor(value, dtype=torch.float32) for value in loaded_embed.values()]
        self.atom_embeddings = torch.stack(embed_list, dim=0)  # [167, 167]

        self.a_init = self.atom_embeddings.shape[-1]  # Dim atom embedding init = 167
        self.b_init = kwargs.get('h_b')  # Dim bond embedding init

        assert self.a_init is not None and self.b_init is not None

        self.h_a = kwargs.get('h_a', 64)  # Dim of the hidden atom embedding learnt
        self.n_conv = kwargs.get('n_conv', 4)  # Number of GCN layers
        self.h_g = kwargs.get('h_g', 32)  # Dim of the hidden graph embedding after pooling
        random_seed = kwargs.get('random_seed', None)  # Seed to fix the simulation

        # The model is defined below
        randomSeed(random_seed)
        # num_embeddings*embedding_dim
        self.embed = nn.Embedding.from_pretrained(self.atom_embeddings,
                                                  freeze=True)  # Load atom embeddings from the one hot atom init
        self.fc_embedding = nn.Linear(self.a_init, self.h_a)
        self.convs = nn.ModuleList([ConvLayer(self.h_a, self.b_init, random_seed=random_seed) for _ in range(self.n_conv)])
        self.conv_to_fc = nn.Linear(self.h_a, self.h_g)
        self.conv_to_fc_activation = nn.ReLU()
        self.fc_out = nn.Linear(self.h_g, 1)
        self.amino_to_fc = nn.Linear(self.h_a, self.h_g)
        self.amino_to_fc_activation = nn.ReLU()
        self.fc_amino_out = nn.Linear(self.h_g, 1)

    def forward(self, inputs):
        """
        Forward pass

        Parameters
        ----------
        inputs: List         [atom_fea,       [B, n_atom]
                               nbr_fea,       [B, n_atom, n_neighbor=50, 43=40+3]
                           nbr_fea_idx,       [B, n_atom, n_neighbor=50]
                        atom_amino_idx,       [B, n_atom]
                             atom_mask]       [B, n_atom]

        Returns
        -------
        out : The prediction for the given batch of protein graphs
        """
        [atom_emb, nbr_emb, nbr_adj_list, atom_amino_idx, atom_mask] = inputs
        # [B, n_atom] -> [B, n_atom, 167]
        lookup_tensor = self.embed(atom_emb.type(torch.long))
        # [B, n_atom, h_a]
        atom_emb = self.fc_embedding(lookup_tensor)  # [1, 23380, 64]
        # [B, n_atom, 1] expend a dimension
        atom_mask = atom_mask.unsqueeze(dim=-1)

        for idx in range(self.n_conv):
            # [B, n_atom, h_a]
            atom_emb *= atom_mask  # to correct non-atom values to 0 which added by padding

            atom_emb = self.convs[idx](atom_emb, nbr_emb, nbr_adj_list, atom_mask)

        # Update the embedding using the mask, to correct non-atom values to 0 which added by padding
        atom_emb *= atom_mask

        # [B, n_aa, h_a] generate reside amino acid level embeddings
        amino_emb, mask_pooled = self.pooling_amino(atom_emb, atom_amino_idx)
        # [B, n_aa, h_g]
        amino_emb = self.amino_to_fc(self.amino_to_fc_activation(amino_emb))
        amino_emb = self.amino_to_fc_activation(amino_emb)

        # generate protein graph level embeddings
        protein_emb = self.pooling(atom_emb, atom_mask)
        protein_emb = self.conv_to_fc(self.conv_to_fc_activation(protein_emb))
        protein_emb = self.conv_to_fc_activation(protein_emb)

        out = [self.fc_out(protein_emb), self.fc_amino_out(amino_emb), mask_pooled]

        return out

    def pooling(self, atom_emb, atom_mask):
        """
        Pooling the atom features to get protein features

        :param atom_emb: [B, n_atom, h_a] Atom embeddings after convolution
        :param atom_mask [B, n_atom, 1]
        """
        summed = torch.sum(atom_emb, dim=1)
        total = atom_mask.sum(dim=1)
        pooled = summed / total
        assert (pooled.shape[0], pooled.shape[1]) == (atom_emb.shape[0], atom_emb.shape[2])

        return pooled

    def pooling_amino(self, atom_emb, atom_amino_idx):
        """
        Pooling the atom features to get residue amino acid features using the atom_amino_idx that contains the mapping

        :param atom_emb: [B, n_atom, h_a] Atom embeddings after convolution
        :param atom_amino_idx [B, n_atom] Mapping from the amino idx to atom idx
        """
        atom_amino_idx = atom_amino_idx.view(-1).type(torch.LongTensor)  # [B*n_atom]
        atom_emb = atom_emb.view(-1, self.h_a)  # [B*n_atom, h_a]

        max_idx = torch.max(atom_amino_idx)  # largest number of this batch
        min_idx = torch.min(atom_amino_idx)  # always 0 in each batch ?

        # [max_idx + 1, 1] all 1
        mask_pooled = atom_amino_idx.new_full(size=(max_idx + 1, 1), fill_value=1, dtype=torch.bool)  # torch>1.2

        mask_pooled[:min_idx] = 0
        # pooled = torch.scatter_add(atom_emb.t(), atom_amino_idx).t()
        # pooled = torch.scatter_add(input=atom_emb.t(), dim=0, index=atom_amino_idx, src=)


        return pooled, mask_pooled

    def save(self, state, is_best, savepath, filename='checkpoint.pth.tar'):
        """Save model checkpoints"""
        torch.save(state, savepath + filename)
        if is_best:
            shutil.copyfile(savepath + filename, savepath + 'model_best.pth.tar')

    @staticmethod
    def mask_remove(out):
        """Internal function to remove masking after generating residue amino acid level embeddings"""
        out[1] = torch.masked_select(out[1].squeeze(), out[2].squeeze()).unsqueeze(1)
        return out

    def fit(self, outputs, targets, protein_ids, pred=False):
        """Train the model one step for given inputs"""

        self.targets = targets
        self.outputs = outputs

        assert self.outputs[1].shape == self.targets[1].unsqueeze(1).shape

        # Calculate MSE loss
        predicted_targets_global = self.outputs[0]
        predicted_targets_local = self.outputs[1]
        predicted_targets = torch.cat([predicted_targets_global, predicted_targets_local])
        original_targets = torch.cat([self.targets[0], self.targets[1].unsqueeze(1)])
        self.loss = self.criterion(predicted_targets, original_targets)

        if not pred:
            self.optimizer.zero_grad()
            self.loss.backward()
            self.optimizer.step()

        # Calculate MAE error
        self.accuracy = []
        self.accuracy.extend([torch.mean(torch.abs(self.outputs[0] - self.targets[0]))])
        self.accuracy.extend([torch.mean(torch.abs(self.outputs[1] - self.targets[1]))])
