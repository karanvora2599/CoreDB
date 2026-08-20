// pybind11 module registration - the only file with a PYBIND11_MODULE
// block; segmentation.cpp/centrality.cpp are pure implementation files
// with no pybind11 dependency of their own.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "centrality.h"
#include "segmentation.h"

namespace py = pybind11;

PYBIND11_MODULE(_native, m) {
    m.doc() = "CoreDB optional native acceleration (see Documentation/ARCHITECTURE.md's Performance section)";

    m.def("detect_changepoints_indices", &detect_changepoints_indices,
          py::call_guard<py::gil_scoped_release>(),
          py::arg("values"), py::arg("min_size"), py::arg("penalty"),
          "Binary segmentation over index-space values; returns sorted split indices.");

    m.def("brandes_betweenness", &brandes_betweenness,
          py::call_guard<py::gil_scoped_release>(),
          py::arg("adjacency"), py::arg("max_depth"),
          "Brandes' betweenness centrality over a local-integer-indexed adjacency list. "
          "Returns raw scores - not yet halved for the undirected double-count.");

    m.def("pagerank", &pagerank,
          py::call_guard<py::gil_scoped_release>(),
          py::arg("out_edges"), py::arg("damping"), py::arg("max_iterations"), py::arg("tol"),
          "Power-iteration PageRank over a local-integer-indexed directed out-edge list.");
}
