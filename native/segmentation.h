#pragma once

#include <vector>

// See segmentation.cpp - literal port of coredb/signal.py's binary
// segmentation index search.
std::vector<int> detect_changepoints_indices(const std::vector<double>& values, int min_size, double penalty);
