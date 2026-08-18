"""Minimal networkx shim for Code2Database.

Provides just enough of the networkx API to run the scanner and builder
without requiring a full networkx installation. Only implements the
subset of APIs actually used by Code2Database scripts.
"""


class NetworkXError(Exception):
    pass


class NetworkXNoPath(NetworkXError):
    pass


class NodeNotFound(NetworkXError):
    pass


class _NodeView:
    """Dict-like view of nodes that supports iteration and .get()/.items()."""

    def __init__(self, nodes_dict):
        self._dict = nodes_dict

    def __iter__(self):
        return iter(self._dict.keys())

    def __len__(self):
        return len(self._dict)

    def __contains__(self, key):
        return key in self._dict

    def __getitem__(self, key):
        return self._dict[key]

    def get(self, key, default=None):
        return self._dict.get(key, default)

    def items(self):
        return list(self._dict.items())

    def keys(self):
        return list(self._dict.keys())

    def values(self):
        return list(self._dict.values())

    def __call__(self, data=False):
        """Support G.nodes(data=True) pattern."""
        if data:
            return list(self._dict.items())
        return list(self._dict.keys())


class _AdjView:
    """Dict-like view for adjacency (edge) access: G[u][v]."""

    def __init__(self, adj_dict):
        self._dict = adj_dict

    def __getitem__(self, key):
        return self._dict[key]

    def __contains__(self, key):
        return key in self._dict


class DiGraph:
    """Minimal directed graph implementation compatible with networkx.DiGraph API."""

    def __init__(self, **kwargs):
        self._nodes = {}   # node_id -> {**attrs}
        self._succ = {}    # node_id -> {target_id -> {**edge_attrs}}
        self._pred = {}    # node_id -> {source_id -> {**edge_attrs}}
        self.graph = {}

    # --- Node operations ---

    def add_node(self, node_id, **attrs):
        if node_id not in self._nodes:
            self._nodes[node_id] = {}
            self._succ[node_id] = {}
            self._pred[node_id] = {}
        self._nodes[node_id].update(attrs)

    def remove_node(self, node_id):
        if node_id not in self._nodes:
            raise NetworkXError(f"Node {node_id} not in graph")
        # Remove all edges pointing to/from this node
        for pred in list(self._pred[node_id]):
            del self._succ[pred][node_id]
        for succ in list(self._succ[node_id]):
            del self._pred[succ][node_id]
        del self._nodes[node_id]
        del self._succ[node_id]
        del self._pred[node_id]

    def has_node(self, node_id):
        return node_id in self._nodes

    def add_nodes_from(self, nodes_for_adding, **attrs):
        """Add multiple nodes. Accepts iterable of node IDs or (node_id, attrs) tuples."""
        for node in nodes_for_adding:
            if isinstance(node, tuple):
                nid, ndata = node
                self.add_node(nid, **ndata)
            else:
                self.add_node(node, **attrs)

    @property
    def nodes(self):
        """Return nodes. Supports dict-like access via .get() and items()."""
        return _NodeView(self._nodes)

    def number_of_nodes(self):
        return len(self._nodes)

    # --- Edge operations ---

    def add_edge(self, u, v, **attrs):
        if u not in self._nodes:
            self.add_node(u)
        if v not in self._nodes:
            self.add_node(v)
        if v in self._succ[u]:
            self._succ[u][v].update(attrs)
        else:
            edge_attrs = dict(attrs)
            self._succ[u][v] = edge_attrs
            self._pred[v][u] = edge_attrs  # same dict object so mutations stay in sync

    def remove_edge(self, u, v):
        if u in self._succ and v in self._succ[u]:
            del self._succ[u][v]
        if v in self._pred and u in self._pred[v]:
            del self._pred[v][u]

    def has_edge(self, u, v):
        return u in self._succ and v in self._succ[u]

    def in_edges(self, nbunch=None, data=False):
        """Return incoming edges. Supports nbunch (node or iterable of nodes) and data flag."""
        if nbunch is None:
            result = []
            for v, sources in self._pred.items():
                for u, attrs in sources.items():
                    result.append((u, v, attrs) if data else (u, v))
            return result
        if not hasattr(nbunch, '__iter__') or isinstance(nbunch, str):
            nbunch = [nbunch]
        result = []
        for v in nbunch:
            if v in self._pred:
                for u, attrs in self._pred[v].items():
                    result.append((u, v, attrs) if data else (u, v))
        return result

    def out_edges(self, nbunch=None, data=False):
        """Return outgoing edges. Supports nbunch (node or iterable of nodes) and data flag."""
        if nbunch is None:
            result = []
            for u, targets in self._succ.items():
                for v, attrs in targets.items():
                    result.append((u, v, attrs) if data else (u, v))
            return result
        if not hasattr(nbunch, '__iter__') or isinstance(nbunch, str):
            nbunch = [nbunch]
        result = []
        for u in nbunch:
            if u in self._succ:
                for v, attrs in self._succ[u].items():
                    result.append((u, v, attrs) if data else (u, v))
        return result

    def edges(self, data=False, nbunch=None):
        if nbunch is not None:
            if not hasattr(nbunch, '__iter__'):
                nbunch = [nbunch]
            result = []
            for u in nbunch:
                if u in self._succ:
                    for v, attrs in self._succ[u].items():
                        result.append((u, v, attrs) if data else (u, v))
            return result
        result = []
        for u, targets in self._succ.items():
            for v, attrs in targets.items():
                result.append((u, v, attrs) if data else (u, v))
        return result

    def number_of_edges(self):
        return sum(len(targets) for targets in self._succ.values())

    def get_edge_data(self, u, v, default=None):
        if u in self._succ and v in self._succ[u]:
            return self._succ[u][v]
        return default

    # --- Neighbor operations ---

    def successors(self, node_id):
        if node_id not in self._succ:
            raise NetworkXError(f"Node {node_id} not in graph")
        return list(self._succ[node_id].keys())

    def predecessors(self, node_id):
        if node_id not in self._pred:
            raise NetworkXError(f"Node {node_id} not in graph")
        return list(self._pred[node_id].keys())

    def neighbors(self, node_id):
        return self.successors(node_id)

    def degree(self, nbunch=None):
        if nbunch is None:
            return [(n, len(self._succ[n]) + len(self._pred[n])) for n in self._nodes]
        if not hasattr(nbunch, '__iter__') or isinstance(nbunch, str):
            return len(self._succ.get(nbunch, {})) + len(self._pred.get(nbunch, {}))
        return [(n, len(self._succ.get(n, {})) + len(self._pred.get(n, {}))) for n in nbunch]

    def out_degree(self, nbunch=None):
        if nbunch is None:
            return [(n, len(succs)) for n, succs in self._succ.items()]
        if not hasattr(nbunch, '__iter__') or isinstance(nbunch, str):
            return len(self._succ.get(nbunch, {}))
        return [(n, len(self._succ.get(n, {}))) for n in nbunch]

    def in_degree(self, nbunch=None):
        if nbunch is None:
            return [(n, len(preds)) for n, preds in self._pred.items()]
        if not hasattr(nbunch, '__iter__') or isinstance(nbunch, str):
            return len(self._pred.get(nbunch, {}))
        return [(n, len(self._pred.get(n, {}))) for n in nbunch]

    # --- Special methods ---

    def __contains__(self, node_id):
        return node_id in self._nodes

    def __len__(self):
        return len(self._nodes)

    def __iter__(self):
        return iter(self._nodes)

    def __getitem__(self, key):
        """Support G[u][v] for edge access."""
        if key in self._nodes:
            return _AdjView(self._succ[key])
        raise KeyError(key)

    def is_directed(self):
        """Return True — DiGraph is always directed."""
        return True


def shortest_path(G, source, target=None):
    """BFS shortest path from source to target (or all reachable nodes)."""
    if source not in G:
        raise NodeNotFound(f"Node {source} not in graph")

    from collections import deque
    visited = {source: [source]}
    queue = deque([source])
    while queue:
        current = queue.popleft()
        for neighbor in G.successors(current):
            if neighbor not in visited:
                visited[neighbor] = visited[current] + [neighbor]
                if target is not None and neighbor == target:
                    return visited[neighbor]
                queue.append(neighbor)

    if target is not None:
        raise NetworkXNoPath(f"No path from {source} to {target}")
    return visited


def has_path(G, source, target):
    """Check if path exists from source to target."""
    try:
        shortest_path(G, source, target)
        return True
    except (NetworkXNoPath, NodeNotFound):
        return False


def all_simple_paths(G, source, target, cutoff=None):
    """DFS all simple paths from source to target."""
    if source not in G:
        raise NodeNotFound(f"Node {source} not in graph")
    if target not in G:
        raise NodeNotFound(f"Node {target} not in graph")

    def _dfs(current, path, visited):
        if cutoff is not None and len(path) > cutoff:
            return
        if current == target:
            yield list(path)
            return
        for neighbor in G.successors(current):
            if neighbor not in visited:
                visited.add(neighbor)
                path.append(neighbor)
                yield from _dfs(neighbor, path, visited)
                path.pop()
                visited.remove(neighbor)

    yield from _dfs(source, [source], {source})


def betweenness_centrality(G, normalized=True, k=None):
    """Approximate betweenness centrality using sampling."""
    import random
    nodes = list(G.nodes())
    if not nodes:
        return {}

    if k is not None and k < len(nodes):
        sample = random.sample(nodes, min(k, len(nodes)))
    else:
        sample = nodes

    bc = {n: 0.0 for n in nodes}
    from collections import deque
    for s in sample:
        # BFS from s
        pred = {s: []}
        dist = {s: 0}
        sigma = {s: 1}
        queue = deque([s])
        while queue:
            v = queue.popleft()
            for w in G.successors(v):
                if w not in dist:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist.get(w) == dist[v] + 1:
                    sigma[w] = sigma.get(w, 0) + sigma.get(v, 1)
                    pred.setdefault(w, []).append(v)

        # Accumulate
        delta = {n: 0.0 for n in nodes}
        stack = sorted(dist.keys(), key=lambda x: -dist[x])
        for w in stack:
            for v in pred.get(w, []):
                delta[v] += (sigma.get(v, 1) / max(sigma.get(w, 1), 1)) * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]

    if normalized and len(nodes) > 2:
        norm = (len(nodes) - 1) * (len(nodes) - 2)
        if G.is_directed():
            norm = (len(nodes) - 1) * (len(nodes) - 2)
        bc = {n: v / norm for n, v in bc.items()}

    return bc


def descendants(G, source):
    """Return all nodes reachable from source."""
    if source not in G:
        raise NodeNotFound(f"Node {source} not in graph")
    from collections import deque
    visited = set()
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for succ in G.successors(node):
            if succ not in visited:
                visited.add(succ)
                queue.append(succ)
    return visited


def predecessor(G, source, target=None, cutoff=None, return_seen=False):
    """BFS from ``source`` over a directed graph, returning a dict
    ``{node: [parent, ...]}`` describing the BFS predecessor tree.

    Mirrors ``networkx.predecessor``. The caller reconstructs paths by
    walking parent pointers (e.g. ``cur = pred_map[cur][0]`` back to
    ``source``). On a 700K-node graph this is O(V+E) per source — far
    cheaper than calling ``shortest_path`` per source/target pair.
    """
    if source not in G:
        raise NodeNotFound(f"Node {source} not in graph")
    from collections import deque
    # pred_map[node] = list of parents (BFS may discover a node via multiple
    # parents at the same level; we keep the first one to mirror shortest_path
    # semantics for path reconstruction).
    pred_map = {source: []}
    seen = {source: 0}
    queue = deque([(source, 0)])
    while queue:
        node, level = queue.popleft()
        if cutoff is not None and level >= cutoff:
            continue
        for succ in G.successors(node):
            if succ not in seen:
                seen[succ] = level + 1
                pred_map[succ] = [node]
                queue.append((succ, level + 1))
                if target is not None and succ == target:
                    if return_seen:
                        return pred_map, seen
                    return pred_map
            elif seen[succ] == level + 1:
                # Same-level parent — append for completeness.
                pred_map[succ].append(node)
    if return_seen:
        return pred_map, seen
    return pred_map


def bfs_predecessors(G, source, cutoff=None):
    """Yield ``(node, parent)`` pairs from a BFS rooted at ``source``.

    Mirrors ``networkx.bfs_predecessors``. The source itself is skipped
    (it has no predecessor).
    """
    if source not in G:
        raise NodeNotFound(f"Node {source} not in graph")
    from collections import deque
    seen = {source: 0}
    queue = deque([(source, 0)])
    while queue:
        node, level = queue.popleft()
        if cutoff is not None and level >= cutoff:
            continue
        for succ in G.successors(node):
            if succ not in seen:
                seen[succ] = level + 1
                yield (succ, node)
                queue.append((succ, level + 1))


def ancestors(G, source):
    """Return all nodes that can reach ``source`` (reverse BFS)."""
    if source not in G:
        raise NodeNotFound(f"Node {source} not in graph")
    from collections import deque
    visited = set()
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for pred in G.predecessors(node):
            if pred not in visited:
                visited.add(pred)
                queue.append(pred)
    return visited


def compose(G1, G2):
    """Compose G1 and G2. G2 takes priority for overlapping nodes/edges."""
    result = DiGraph()
    # Add G1 nodes and edges
    for nid, attrs in G1.nodes(data=True):
        result.add_node(nid, **attrs)
    for u, v, attrs in G1.edges(data=True):
        result.add_edge(u, v, **attrs)
    # Add G2 nodes and edges (overwrites G1)
    for nid, attrs in G2.nodes(data=True):
        result.add_node(nid, **attrs)
    for u, v, attrs in G2.edges(data=True):
        result.add_edge(u, v, **attrs)
    # Merge graph attributes (G2 overrides G1)
    result.graph.update(G1.graph)
    result.graph.update(G2.graph)
    return result


def is_directed(G):
    """Check if graph is directed."""
    return isinstance(G, DiGraph)


def weakly_connected_components(G):
    """Return weakly connected components as sets of nodes."""
    if not isinstance(G, DiGraph):
        raise NetworkXError("Only DiGraph supported")
    visited = set()
    components = []
    for node in G:
        if node not in visited:
            component = set()
            stack = [node]
            while stack:
                n = stack.pop()
                if n in visited:
                    continue
                visited.add(n)
                component.add(n)
                # In weakly connected, traverse both successors and predecessors
                for succ in G.successors(n):
                    if succ not in visited:
                        stack.append(succ)
                for pred in G.predecessors(n):
                    if pred not in visited:
                        stack.append(pred)
            components.append(component)
    return components


def strongly_connected_components(G):
    """Return strongly connected components using Tarjan's algorithm."""
    if not isinstance(G, DiGraph):
        raise NetworkXError("Only DiGraph supported")
    index_counter = [0]
    stack = []
    lowlink = {}
    index = {}
    on_stack = {}
    result = []

    def strongconnect(v):
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True

        for w in G.successors(v):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w, False):
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            component = set()
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.add(w)
                if w == v:
                    break
            result.append(component)

    for v in G:
        if v not in index:
            strongconnect(v)

    return result
