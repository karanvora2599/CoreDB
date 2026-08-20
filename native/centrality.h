#pragma once

#include <vector>

// See centrality.cpp - literal ports of engine.py's betweenness_all()/
// pagerank_all(), operating on a plain local-integer-indexed adjacency
// list instead of entity-id strings + LMDB (that glue stays in
// coredb/graph_algorithms.py, the only place with database access).

// Brandes' algorithm (unweighted, undirected, BFS bounded by max_depth).
// adjacency[i] = neighbor indices of node i. Returns raw per-node scores -
// NOT yet halved for the undirected double-count, matching
// betweenness_all()'s existing contract (the caller halves).
std::vector<double> brandes_betweenness(const std::vector<std::vector<int>>& adjacency, int max_depth);

// Power-iteration PageRank. out_edges[i] = out-neighbor indices of node i
// (directed). Dangling nodes (empty out_edges) redistribute their rank
// uniformly across every node, one node at a time in index order - same
// operation sequence as engine.py's pagerank_all(), not a "sum once"
// shortcut, so results match to the same floating-point rounding.
std::vector<double> pagerank(const std::vector<std::vector<int>>& out_edges, double damping,
                              int max_iterations, double tol);
