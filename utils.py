import os
import math
import networkx as nx
import numpy as np
import scipy as sp
import scipy.sparse
import torch
import torch.nn.functional as F
# import community as community_louvain
from networkx.algorithms import community

from torch import Tensor
from torch.utils.data import Dataset

# from grakel.utils import graph_from_networkx
# from grakel.kernels import WeisfeilerLehman, VertexHistogram
from tqdm import tqdm
import scipy.sparse as sparse
from torch_geometric.data import Data

from extract_feats import extract_feats, extract_numbers, augment_graph_features



def preprocess_dataset(dataset, n_max_nodes, spectral_emb_dim):

    data_lst = []
    if dataset == 'test':
        filename = '../../code/data/dataset_'+dataset+'.pt'
        desc_file = '../../code/data/'+dataset+'/test.txt'

        if os.path.isfile(filename):
            data_lst = torch.load(filename)
            print(f'Dataset {filename} loaded from file')

        else:
            fr = open(desc_file, "r")
            for line in fr:
                line = line.strip()
                tokens = line.split(",")
                graph_id = tokens[0]
                desc = tokens[1:]
                desc = "".join(desc)
                # feats_stats = augment_graph_features(desc)
                feats_stats = extract_numbers(desc)
                feats_stats = augment_graph_features(feats_stats)
                feats_stats = torch.FloatTensor(feats_stats).unsqueeze(0)
                data_lst.append(Data(stats=feats_stats, filename = graph_id))
            fr.close()
            torch.save(data_lst, filename)
            print(f'Dataset {filename} saved')


    else:
        filename = '../../code/data/dataset_'+dataset+'.pt'
        graph_path = '../../code/data/'+dataset+'/graph'
        desc_path = '../../code/data/'+dataset+'/description'

        if os.path.isfile(filename):
            data_lst = torch.load(filename)
            print(f'Dataset {filename} loaded from file')

        else:
            # traverse through all the graphs of the folder
            files = [f for f in os.listdir(graph_path)]
            adjs = []
            eigvals = []
            eigvecs = []
            n_nodes = []
            max_eigval = 0
            min_eigval = 0
            for fileread in tqdm(files):
                tokens = fileread.split("/")
                idx = tokens[-1].find(".")
                filen = tokens[-1][:idx]
                extension = tokens[-1][idx+1:]
                fread = os.path.join(graph_path,fileread)
                fstats = os.path.join(desc_path,filen+".txt")
                #load dataset to networkx
                if extension=="graphml":
                    G = nx.read_graphml(fread)
                    # Convert node labels back to tuples since GraphML stores them as strings
                    G = nx.convert_node_labels_to_integers(
                        G, ordering="sorted"
                    )
                else:
                    G = nx.read_edgelist(fread)
                # use canonical order (BFS) to create adjacency matrix
                ### BFS & DFS from largest-degree node

                
                CGs = [G.subgraph(c) for c in nx.connected_components(G)]

                # rank connected componets from large to small size
                CGs = sorted(CGs, key=lambda x: x.number_of_nodes(), reverse=True)

                node_list_bfs = []
                for ii in range(len(CGs)):
                    node_degree_list = [(n, d) for n, d in CGs[ii].degree()]
                    degree_sequence = sorted(
                    node_degree_list, key=lambda tt: tt[1], reverse=True)

                    bfs_tree = nx.bfs_tree(CGs[ii], source=degree_sequence[0][0])
                    node_list_bfs += list(bfs_tree.nodes())

                adj_bfs = nx.to_numpy_array(G, nodelist=node_list_bfs)

                adj = torch.from_numpy(adj_bfs).float()
                diags = np.sum(adj_bfs, axis=0)
                diags = np.squeeze(np.asarray(diags))
                D = sparse.diags(diags).toarray()
                L = D - adj_bfs
                with np.errstate(divide="ignore"):
                    diags_sqrt = 1.0 / np.sqrt(diags)
                diags_sqrt[np.isinf(diags_sqrt)] = 0
                DH = sparse.diags(diags).toarray()
                L = np.linalg.multi_dot((DH, L, DH))
                L = torch.from_numpy(L).float()
                eigval, eigvecs = torch.linalg.eigh(L)
                eigval = torch.real(eigval)
                eigvecs = torch.real(eigvecs)
                idx = torch.argsort(eigval)
                eigvecs = eigvecs[:,idx]

                edge_index = torch.nonzero(adj).t()

                size_diff = n_max_nodes - G.number_of_nodes()
                x = torch.zeros(G.number_of_nodes(), spectral_emb_dim+1)
                x[:,0] = torch.mm(adj, torch.ones(G.number_of_nodes(), 1))[:,0]/(n_max_nodes-1)
                mn = min(G.number_of_nodes(),spectral_emb_dim)
                mn+=1
                x[:,1:mn] = eigvecs[:,:spectral_emb_dim]
                adj = F.pad(adj, [0, size_diff, 0, size_diff])
                adj = adj.unsqueeze(0)

                # feats_stats = augment_graph_features(fstats)
                feats_stats = extract_feats(fstats)
                feats_stats = augment_graph_features(feats_stats)
                feats_stats = torch.FloatTensor(feats_stats).unsqueeze(0)

                data_lst.append(Data(x=x, edge_index=edge_index, A=adj, stats=feats_stats, filename = filen))
            torch.save(data_lst, filename)
            print(f'Dataset {filename} saved')
    return data_lst


def construct_nx_from_adj(adj):
    G = nx.from_numpy_array(adj, create_using=nx.Graph)
    to_remove = []
    for node in G.nodes():
        if G.degree(node) == 0:
            to_remove.append(node)
    G.remove_nodes_from(to_remove)
    return G

def handle_nan(x):
    if math.isnan(x):
        return float(-100)
    return x

def masked_instance_norm2D(x: torch.Tensor, mask: torch.Tensor, eps: float = 1e-5):
    """
    x: [batch_size (N), num_objects (L), num_objects (L), features(C)]
    mask: [batch_size (N), num_objects (L), num_objects (L), 1]
    """
    mask = mask.view(x.size(0), x.size(1), x.size(2), 1).expand_as(x)
    mean = (torch.sum(x * mask, dim=[1,2]) / torch.sum(mask, dim=[1,2]))   # (N,C)
    var_term = ((x - mean.unsqueeze(1).unsqueeze(1).expand_as(x)) * mask)**2  # (N,L,L,C)
    var = (torch.sum(var_term, dim=[1,2]) / torch.sum(mask, dim=[1,2]))  # (N,C)
    mean = mean.unsqueeze(1).unsqueeze(1).expand_as(x)  # (N, L, L, C)
    var = var.unsqueeze(1).unsqueeze(1).expand_as(x)    # (N, L, L, C)
    instance_norm = (x - mean) / torch.sqrt(var + eps)   # (N, L, L, C)
    instance_norm = instance_norm * mask
    return instance_norm

def masked_layer_norm2D(x: torch.Tensor, mask: torch.Tensor, eps: float = 1e-5):
    """
    x: [batch_size (N), num_objects (L), num_objects (L), features(C)]
    mask: [batch_size (N), num_objects (L), num_objects (L), 1]
    """
    mask = mask.view(x.size(0), x.size(1), x.size(2), 1).expand_as(x)
    mean = torch.sum(x * mask, dim=[3,2,1]) / torch.sum(mask, dim=[3,2,1])   # (N)
    var_term = ((x - mean.view(-1,1,1,1).expand_as(x)) * mask)**2  # (N,L,L,C)
    var = (torch.sum(var_term, dim=[3,2,1]) / torch.sum(mask, dim=[3,2,1]))  # (N)
    mean = mean.view(-1,1,1,1).expand_as(x)  # (N, L, L, C)
    var = var.view(-1,1,1,1).expand_as(x)    # (N, L, L, C)
    layer_norm = (x - mean) / torch.sqrt(var + eps)   # (N, L, L, C)
    layer_norm = layer_norm * mask
    return layer_norm


def cosine_beta_schedule(timesteps, s=0.008):
    """
    cosine schedule as proposed in https://arxiv.org/abs/2102.09672
    """
    steps = timesteps + 1
    x = torch.linspace(0, timesteps, steps)
    alphas_cumprod = torch.cos(((x / timesteps) + s) / (1 + s) * torch.pi * 0.5) ** 2
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return torch.clip(betas, 0.0001, 0.9999)


def linear_beta_schedule(timesteps):
    beta_start = 0.0001
    beta_end = 0.02
    return torch.linspace(beta_start, beta_end, timesteps)


def quadratic_beta_schedule(timesteps):
    beta_start = 0.0001
    beta_end = 0.02
    return torch.linspace(beta_start**0.5, beta_end**0.5, timesteps) ** 2


def sigmoid_beta_schedule(timesteps):
    beta_start = 0.0001
    beta_end = 0.02
    betas = torch.linspace(-6, 6, timesteps)
    return torch.sigmoid(betas) * (beta_end - beta_start) + beta_start


def extract_graph_properties(edges):
    G = nx.Graph()
    G.add_edges_from(edges)
    
    nb_of_nodes = G.number_of_nodes()
    nb_of_edges = G.number_of_edges()
    average_degree = 2 * nb_of_edges / nb_of_nodes if nb_of_nodes > 0 else 0
    nb_of_triangles = sum(nx.triangles(G).values()) // 3
    global_clustering_coefficient = nx.transitivity(G)
    maximum_k_core = max(nx.core_number(G).values()) if nb_of_nodes > 0 else 0
    nb_of_communities = len(list(community.greedy_modularity_communities(G)))
    
    return [nb_of_nodes, nb_of_edges, average_degree, nb_of_triangles, global_clustering_coefficient, maximum_k_core, nb_of_communities]


def normalize_property(value, prop_name):
    # Define property ranges (update if needed)
    property_ranges = {
        "nb_of_nodes": (10, 50),
        "nb_of_edges": (9, 1188),
        "average_degree": (1, 48),
        "nb_of_triangles": (0, 18067),
        "global_clustering_coefficient": (0, 1),
        "maximum_k_core": (1, 47),
        "nb_of_communities": (1, 16),
    }
    min_val, max_val = property_ranges[prop_name]
    return (value - min_val) / (max_val - min_val)


def normalize_properties(properties):
    keys = ["nb_of_nodes", "nb_of_edges", "average_degree", "nb_of_triangles", "global_clustering_coefficient", "maximum_k_core", "nb_of_communities"]
    return [normalize_property(value, key) for value, key in zip(properties, keys)]


def extract_graph_properties_from_adj(adj):
    batch_size, n_nodes, _ = adj.shape
    properties_batch = []
    for i in range(batch_size):
        # Convert adjacency matrix to edge list
        edges = torch.nonzero(adj[i], as_tuple=False).tolist()
        properties = extract_graph_properties(edges)
        properties_batch.append(properties)
    return properties_batch


def normalize(value, min_val, max_val):
    return (value - min_val) / (max_val - min_val)
