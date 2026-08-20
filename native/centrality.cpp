#include "centrality.h"

#include <cmath>
#include <deque>

std::vector<double> brandes_betweenness(const std::vector<std::vector<int>>& adjacency, int max_depth) {
    int n = static_cast<int>(adjacency.size());
    std::vector<double> betweenness(n, 0.0);

    for (int s = 0; s < n; ++s) {
        std::vector<int> stack;
        stack.reserve(n);
        std::vector<std::vector<int>> predecessors(n);
        std::vector<long long> sigma(n, 0);
        std::vector<int> dist(n, -1);
        sigma[s] = 1;
        dist[s] = 0;

        std::deque<int> queue;
        queue.push_back(s);
        while (!queue.empty()) {
            int v = queue.front();
            queue.pop_front();
            stack.push_back(v);
            if (dist[v] >= max_depth) {
                continue;
            }
            for (int w : adjacency[v]) {
                if (dist[w] < 0) {
                    dist[w] = dist[v] + 1;
                    queue.push_back(w);
                }
                if (dist[w] == dist[v] + 1) {
                    sigma[w] += sigma[v];
                    predecessors[w].push_back(v);
                }
            }
        }

        std::vector<double> delta(n, 0.0);
        for (auto it = stack.rbegin(); it != stack.rend(); ++it) {
            int w = *it;
            for (int v : predecessors[w]) {
                delta[v] += (static_cast<double>(sigma[v]) / static_cast<double>(sigma[w])) * (1.0 + delta[w]);
            }
            if (w != s) {
                betweenness[w] += delta[w];
            }
        }
    }
    return betweenness;
}

std::vector<double> pagerank(const std::vector<std::vector<int>>& out_edges, double damping,
                              int max_iterations, double tol) {
    int n = static_cast<int>(out_edges.size());
    if (n == 0) {
        return {};
    }
    std::vector<double> rank(n, 1.0 / n);

    for (int iter = 0; iter < max_iterations; ++iter) {
        std::vector<double> new_rank(n, (1.0 - damping) / n);
        for (int v = 0; v < n; ++v) {
            const auto& out = out_edges[v];
            if (out.empty()) {
                double share = damping * rank[v] / n;
                for (int u = 0; u < n; ++u) {
                    new_rank[u] += share;
                }
            } else {
                double share = damping * rank[v] / static_cast<double>(out.size());
                for (int u : out) {
                    new_rank[u] += share;
                }
            }
        }
        double diff = 0.0;
        for (int v = 0; v < n; ++v) {
            diff += std::fabs(new_rank[v] - rank[v]);
        }
        rank = std::move(new_rank);
        if (diff < tol) {
            break;
        }
    }
    return rank;
}
