import torch
import numpy as np
import torch.nn.functional as tnfunc
from tqdm import tqdm, trange
from scipy.stats import spearmanr, kendalltau
import networkx as nx

from dataset import Dataset
from layers import AvePoolingModule, AttentionModule, TenorNetworkModule, NodeGraphMatchingModule, MT_NEGCN
from utils import calculate_loss


class GraphSim(torch.nn.Module):

    def __init__(self, args, number_of_node_labels, number_of_edge_labels):
        super(GraphSim, self).__init__()
        self.args = args

        self.number_node_labels = number_of_node_labels
        self.number_edge_labels = number_of_edge_labels
        self.setup_layers()

    def calculate_bottleneck_features(self):
        tensor_layer_out = self.args.tensor_neurons * 2

        if self.args.histogram:
            self.feature_count = tensor_layer_out + self.args.bins
        else:
            self.feature_count = tensor_layer_out

        # node-graph-features
        if self.args.node_graph_matching:
            self.feature_count = self.feature_count + self.args.hidden_size * 4

    def setup_layers(self):
        self.calculate_bottleneck_features()

        self.convolution_0 = MT_NEGCN(self.args, self.number_node_labels, self.number_edge_labels)

        if self.args.attention_module:
            self.attention = AttentionModule(self.args).to(self.args.device)
            self.attention_edge = AttentionModule(self.args).to(self.args.device)
        else:
            self.avePooling = AvePoolingModule(self.args).to(self.args.device)

        self.tensor_network = TenorNetworkModule(self.args).to(self.args.device)

        if self.args.node_graph_matching:
            self.node_graph_matching = NodeGraphMatchingModule(self.args).to(self.args.device)

        self.fully_connected_first = torch.nn.Linear(self.feature_count,
                                                     self.args.bottle_neck_neurons).to(self.args.device)
        self.scoring_layer = torch.nn.Linear(self.args.bottle_neck_neurons, 1).to(self.args.device)

    def calculate_histogram(self, abstract_features_1, abstract_features_2):
        scores = torch.mm(abstract_features_1, abstract_features_2).detach().to(self.args.device)
        scores = scores.view(-1, 1)
        hist = torch.histc(scores, bins=self.args.bins).to(self.args.device)
        hist = hist / torch.sum(hist)
        hist = hist.view(1, -1)
        return hist

    def forward(self, data):

        abstract_features_1, edge_features_1 = self.convolution_0(data["node_features_1"], data["edge_index_1"],
                                                                  data["edge_features_1"], data["trans_edge_index_1"])
        abstract_features_2, edge_features_2 = self.convolution_0(data["node_features_2"], data["edge_index_2"],
                                                                  data["edge_features_2"], data["trans_edge_index_2"])

        if self.args.histogram:
            hist = self.calculate_histogram(abstract_features_1,
                                            torch.t(abstract_features_2))

        if self.args.tensor_network:
            if self.args.attention_module:

                pooled_edge_features_1 = self.attention_edge(edge_features_1)
                pooled_edge_features_2 = self.attention_edge(edge_features_2)
                pooled_features_1 = self.attention(abstract_features_1)
                pooled_features_2 = self.attention(abstract_features_2)
            else:
                pooled_features_1 = self.avePooling(abstract_features_1)
                pooled_features_2 = self.avePooling(abstract_features_2)
                pooled_edge_features_1 = self.avePooling(edge_features_1)
                pooled_edge_features_2 = self.avePooling(edge_features_2)

            scores_node = self.tensor_network(pooled_features_1, pooled_features_2)
            scores_edge = self.tensor_network(pooled_edge_features_1, pooled_edge_features_2)
            scores = torch.t(torch.cat((scores_node, scores_edge), dim=0))

            if self.args.histogram:
                scores = torch.cat((scores, hist), dim=1).view(1, -1)
        else:
            scores = hist.view(1, -1)

        if self.args.node_graph_matching:
            # node-graph sub-network
            node_graph_score = self.node_graph_matching(abstract_features_1, abstract_features_2)
            scores = torch.cat((scores, node_graph_score), dim=1).view(1, -1)

        scores = tnfunc.relu(self.fully_connected_first(scores))
        score = torch.sigmoid(self.scoring_layer(scores))
        return score


class GraphSimTrainer(object):
    def __init__(self, args):
        self.args = args

        """
        init paths
        """
        self.args.dataset_path = self.args.dataset_root_path + self.args.current_dataset_name + \
                                 "/" + self.args.current_dataset_name + "_dataset.pkl"
        self.args.training_root_path = self.args.dataset_root_path + self.args.current_dataset_name + "/train/"
        self.args.test_root_path = self.args.dataset_root_path + self.args.current_dataset_name + "/test/"
        self.args.ged_path = self.args.dataset_root_path + self.args.current_dataset_name + \
                             "/" + self.args.current_dataset_name + "_ged.pkl"
        self.args.save_path = "../pretrained_models/" + self.args.filename
        self.args.best_model_path = "../pretrained_models/" + self.args.filename + "-best-val"
        self.args.load_path = "../pretrained_models/" + self.args.filename

        self.dataset = Dataset(args)
        self.model = GraphSim(self.args, self.dataset.number_of_node_labels, self.dataset.number_of_edge_labels) \
            .to(self.args.device)

    def create_batches(self):
        batches = []
        for graph_pair_index in range(0, len(self.dataset.training_graph_index_pairs), self.args.batch_size):
            batches.append(
                self.dataset.training_graph_index_pairs[graph_pair_index: graph_pair_index + self.args.batch_size])
        return batches

    def process_batch(self, batch):
        self.optimizer.zero_grad()
        losses = 0
        for graph_index_pair in batch:
            data = self.dataset.get_data(graph_index_pair, mode="training")
            data = self.dataset.transfer_to_torch(data)
            prediction = self.model(data)
            losses = losses + tnfunc.mse_loss(data["target"], prediction)
        losses.backward(retain_graph=True)
        self.optimizer.step()
        loss = losses.item()
        return loss

    def validate(self, index):
        from utils import calculate_normalized_ged
        self.model.eval()
        print("\n\nModel evaluation.\n")
        scores = []
        ground_truth = []
        for graph_index_pair in tqdm(self.dataset.validation_graph_index_pairs):
            data = self.dataset.get_data(graph_index_pair, mode="validation")
            ground_truth.append(calculate_normalized_ged(data))
            data = self.dataset.transfer_to_torch(data)
            target = data["target"]
            prediction = self.model(data)
            scores.append(calculate_loss(prediction, target))
        model_error = np.mean(scores)
        self.epoch_loss_list.append(model_error)
        print("\nModel validate error: " + str(round(float(model_error), 5)) + ".")
        if model_error < self.min_error:
            self.best_epoch_index = index
            self.min_error = model_error
            torch.save(self.model.state_dict(), self.args.best_model_path)

    def train(self):
        print("\nModel training.\n")

        self.optimizer = torch.optim.Adam(self.model.parameters(),
                                          lr=self.args.learning_rate,
                                          weight_decay=self.args.weight_decay)

        epochs = trange(self.args.epochs, leave=True, desc="Epoch")
        self.model.to(self.args.device)

        self.min_error = 100
        self.best_model = None
        self.best_epoch_index = 0
        self.epoch_loss_list = []

        for epoch_index, epoch in enumerate(epochs):
            self.model.train()
            batches = self.create_batches()
            self.loss_sum = 0
            main_index = 0
            for batch in tqdm(batches, total=len(batches), desc="Batches"):
                loss_score = self.process_batch(batch)
                main_index = main_index + len(batch)
                self.loss_sum = self.loss_sum + loss_score * len(batch)
                loss = self.loss_sum / main_index
                epochs.set_description("Epoch (Loss=%g)" % round(loss, 5))
            if self.args.validate:
                self.validate(epoch_index)

        if self.args.validate:
            self.model.load_state_dict(torch.load(self.args.best_model_path))

    def test(self):
        from utils import calculate_ranking_correlation, prec_at_ks

        print("\n\nModel testing.\n")
        self.model.eval()

        scores = np.zeros(len(self.dataset.test_graph_index_pairs))
        self.ground_truth = np.zeros(len(self.dataset.test_graph_index_pairs))
        self.prediction_list = np.zeros(len(self.dataset.test_graph_index_pairs))
        prec_at_10_list = []
        prec_at_20_list = []
        temp_gt = []
        temp_pre = []

        for index, test_index_pair in tqdm(enumerate(self.dataset.test_graph_index_pairs)):
            data = self.dataset.get_data(test_index_pair, mode="test")
            data = self.dataset.transfer_to_torch(data)
            target = data["target"]
            self.ground_truth[index] = target
            prediction = self.model(data)
            self.prediction_list[index] = prediction
            scores[index] = calculate_loss(prediction, target)
            temp_gt.append(target.item())
            temp_pre.append(prediction.item())
            if (index + 1) % len(self.dataset.test_graphs) == 0:
                np_batch_gt = np.array(temp_gt)
                np_batch_p = np.array(temp_pre)
                prec_at_10_list.append(prec_at_ks(np_batch_gt, np_batch_p, 10))
                prec_at_20_list.append(prec_at_ks(np_batch_gt, np_batch_p, 20))
                temp_gt.clear()
                temp_pre.clear()

        mse = np.mean(scores)
        rho = calculate_ranking_correlation(spearmanr, self.prediction_list, self.ground_truth)
        tau = calculate_ranking_correlation(kendalltau, self.prediction_list, self.ground_truth)
        p_at_20 = np.mean(prec_at_20_list)
        p_at_10 = np.mean(prec_at_10_list)
        self.print_evaluation(mse, rho, tau, p_at_20, p_at_10)

    def print_evaluation(self, mse, rho, tau, prec_at_20, prec_at_10):
        mean_ground_truth = np.mean(self.ground_truth)
        mean_predicted = np.mean(self.prediction_list)
        delta = np.mean([(n - mean_ground_truth) ** 2 for n in self.ground_truth])
        predicted_delta = np.mean([(n - mean_predicted) ** 2 for n in self.prediction_list])

        print("\nGround truth delta: " + str(round(float(delta), 8)) + ".")
        print("\nGround truth mean: " + str(round(float(mean_ground_truth), 8)) + ".")
        print("\nPredicted delta: " + str(round(float(predicted_delta), 8)) + ".")
        print("\nPredicted mean: " + str(round(float(mean_predicted), 8)) + ".")
        print("\nModel test error(mse): " + str(round(float(mse), 8)) + ".")
        print("rho: ", rho)
        print("tau: ", tau)
        print("p@20:", prec_at_20)
        print("p@10:", prec_at_10)

        if self.args.validate:
            print("\nModel validation loss in each epoch:", self.epoch_loss_list)
            print("\nBest epoch index: " + str(self.best_epoch_index))
            print("\nBest epoch validate error: " + str(self.min_error))

    def save(self, path=""):
        if path == "":
            path = self.args.save_path
        torch.save(self.model.state_dict(), path)

    def load(self):
        self.model.load_state_dict(torch.load(self.args.load_path))

    def show_ged_query(self, query_graph_index):
        from utils import get_file_id_from_path
        import matplotlib.pyplot as plt

        query_graph_path = self.dataset.test_paths[query_graph_index]
        print("Query graph id: " + get_file_id_from_path(query_graph_path))
        query_graph = nx.read_gexf(query_graph_path)
        nx.draw_networkx(query_graph)
        plt.show()
        tar_test_graph_id = get_file_id_from_path(query_graph_path)

        n_ged_rank_id_list = []
        for key in self.dataset.ged_dict:
            if key[0] == int(tar_test_graph_id):
                key_graph = nx.read_gexf(self.dataset.args.training_root_path + str(key[1]) + ".gexf")
                n_ged = self.dataset.ged_dict[key]
                n_ged = n_ged / (0.5 * (len(query_graph.nodes()) + len(key_graph.nodes())))
                n_ged_rank_id_list.append((key[1], n_ged))

        n_ged_rank_id_list.sort(key=lambda x: x[1])
        for i in range(0, 5):
            g_id = n_ged_rank_id_list[i][0]
            n_ged = n_ged_rank_id_list[i][1]
            key_graph = nx.read_gexf(self.dataset.args.training_root_path + str(g_id) + ".gexf")
            nx.draw_networkx(key_graph)
            plt.show()
            print(str(g_id) + " nGED: " + str(n_ged))

        g_id = n_ged_rank_id_list[400][0]
        n_ged = n_ged_rank_id_list[400][1]
        key_graph = nx.read_gexf(self.dataset.args.training_root_path + str(g_id) + ".gexf")
        nx.draw_networkx(key_graph)
        plt.show()
        print(str(g_id) + " nGED: " + str(n_ged))

        for i in range(1, 6):
            g_id = n_ged_rank_id_list[-i][0]
            n_ged = n_ged_rank_id_list[-i][1]
            key_graph = nx.read_gexf(self.dataset.args.training_root_path + str(g_id) + ".gexf")
            nx.draw_networkx(key_graph)
            plt.show()
            print(str(g_id) + " nGED: " + str(n_ged))

    def show_model_query(self, query_graph_index):
        from utils import get_file_id_from_path
        import matplotlib.pyplot as plt
        self.model.eval()

        query_graph_path = self.dataset.test_paths[query_graph_index]
        print("Query graph id: " + get_file_id_from_path(query_graph_path))
        query_graph = nx.read_gexf(query_graph_path)
        nx.draw_networkx(query_graph)
        plt.show()
        tar_test_graph_id = get_file_id_from_path(query_graph_path)

        n_gSim_rank_id_list = []

        for key in self.dataset.test_graph_index_pairs:
            if key[0] == int(query_graph_index):
                score = self.model(self.dataset.transfer_to_torch(self.dataset.get_data(key, mode="test"))).item()
                n_gSim_rank_id_list.append((get_file_id_from_path(self.dataset.training_paths[key[1]]), score))
        n_gSim_rank_id_list.sort(key=lambda x: x[1])

        for i in range(0, 5):
            g_id = n_gSim_rank_id_list[i][0]
            predicted_similarity = n_gSim_rank_id_list[i][1]
            key_graph = nx.read_gexf(self.args.training_root_path + str(g_id) + ".gexf")
            nx.draw_networkx(key_graph)
            plt.show()
            print(str(g_id) + "predicted similarity: " + str(predicted_similarity))

        g_id = n_gSim_rank_id_list[400][0]
        predicted_similarity = n_gSim_rank_id_list[400][1]
        key_graph = nx.read_gexf(self.args.training_root_path + str(g_id) + ".gexf")
        nx.draw_networkx(key_graph)
        plt.show()
        print(str(g_id) + "predicted similarity: " + str(predicted_similarity))

        for i in range(1, 6):
            g_id = n_gSim_rank_id_list[-i][0]
            predicted_similarity = n_gSim_rank_id_list[-i][1]
            key_graph = nx.read_gexf(self.args.training_root_path + str(g_id) + ".gexf")
            nx.draw_networkx(key_graph)
            plt.show()
            print(str(g_id) + "predicted similarity: " + str(predicted_similarity))

    def show_query(self):
        for i in range(len(self.dataset.test_graphs)):
            self.show_ged_query(i)
            self.show_model_query(i)
