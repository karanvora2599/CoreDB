// Native port of coredb/signal.py's detect_changepoints() index-space
// search (binary segmentation, residual-sum-of-squares cost). A literal,
// faithful port - same accumulation order as the Python _segment_cost/
// _best_split/detect_changepoints, not an algorithmic rewrite (e.g. no
// prefix-sum O(1) segment cost) - so the benchmark's before/after is
// attributable to one variable (native vs Python), not a mix of that and
// an algorithm change. Date handling, None-dropping, and penalty
// auto-computation all stay in coredb/signal.py; this only takes/returns
// plain numeric index-space data.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace {

double segment_cost(const std::vector<double>& values, int start, int end) {
    int n = end - start;
    if (n <= 0) {
        return 0.0;
    }
    double sum = 0.0;
    for (int i = start; i < end; ++i) {
        sum += values[i];
    }
    double mean = sum / n;
    double cost = 0.0;
    for (int i = start; i < end; ++i) {
        double d = values[i] - mean;
        cost += d * d;
    }
    return cost;
}

// Mirrors _best_split(): the split point in (start, end) that most reduces
// total cost, and the size of that reduction. best_split == -1 means no
// split of at least min_size on each side improved on leaving it whole -
// the Python None sentinel, since indices can't be negative here.
std::pair<int, double> best_split(const std::vector<double>& values, int start, int end, int min_size) {
    double base_cost = segment_cost(values, start, end);
    double best_gain = 0.0;
    int best = -1;
    for (int split = start + min_size; split <= end - min_size; ++split) {
        double cost = segment_cost(values, start, split) + segment_cost(values, split, end);
        double gain = base_cost - cost;
        if (gain > best_gain) {
            best_gain = gain;
            best = split;
        }
    }
    return {best, best_gain};
}

}  // namespace

// Mirrors detect_changepoints()'s stack-based recursion over already-
// resolved (no-None, penalty-defaulted) index-space data.
std::vector<int> detect_changepoints_indices(const std::vector<double>& values, int min_size, double penalty) {
    int n = static_cast<int>(values.size());
    std::vector<int> changepoints;
    if (n < 2 * min_size) {
        return changepoints;
    }

    std::vector<std::pair<int, int>> stack;
    stack.push_back({0, n});
    while (!stack.empty()) {
        auto [start, end] = stack.back();
        stack.pop_back();
        if (end - start < 2 * min_size) {
            continue;
        }
        auto [split, gain] = best_split(values, start, end, min_size);
        if (split < 0 || gain <= penalty) {
            continue;
        }
        changepoints.push_back(split);
        stack.push_back({start, split});
        stack.push_back({split, end});
    }
    std::sort(changepoints.begin(), changepoints.end());
    return changepoints;
}

PYBIND11_MODULE(_native, m) {
    m.doc() = "CoreDB optional native acceleration (see Documentation/ARCHITECTURE.md's Performance section)";
    m.def("detect_changepoints_indices", &detect_changepoints_indices,
          py::call_guard<py::gil_scoped_release>(),
          py::arg("values"), py::arg("min_size"), py::arg("penalty"),
          "Binary segmentation over index-space values; returns sorted split indices.");
}
