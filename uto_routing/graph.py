from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from uto_routing.models import Dataset, Edge, Node, Route


@dataclass
class GraphIndex:
    nodes: dict[int, Node]
    adjacency: dict[int, list[tuple[int, float]]]


class RoadGraph:
    def __init__(self, nodes: list[Node], edges: list[Edge]) -> None:
        self.index = self._build_index(nodes, edges)
        self._route_cache: dict[tuple[int, int], Route] = {}

    @staticmethod
    def _build_index(nodes: list[Node], edges: list[Edge]) -> GraphIndex:
        node_lookup = {node.node_id: node for node in nodes}
        adjacency = {node.node_id: [] for node in nodes}
        for edge in edges:
            adjacency.setdefault(edge.source, []).append((edge.target, edge.weight_m))
        return GraphIndex(nodes=node_lookup, adjacency=adjacency)

    @classmethod
    def from_dataset(cls, dataset: Dataset) -> "RoadGraph":
        return cls(dataset.nodes, dataset.edges)

    def snap_to_node(self, lon: float, lat: float) -> int:
        best_node_id = -1
        best_distance = float("inf")
        for node_id, node in self.index.nodes.items():
            distance = (node.lon - lon) ** 2 + (node.lat - lat) ** 2
            if distance < best_distance:
                best_distance = distance
                best_node_id = node_id
        if best_node_id == -1:
            raise ValueError("Graph does not contain any nodes.")
        return best_node_id

    def shortest_path(self, start_node: int, end_node: int) -> Route:
        cache_key = (start_node, end_node)
        if cache_key in self._route_cache:
            return self._route_cache[cache_key]

        if start_node not in self.index.nodes:
            raise ValueError(f"Unknown start node: {start_node}")
        if end_node not in self.index.nodes:
            raise ValueError(f"Unknown end node: {end_node}")

        queue: list[tuple[float, int]] = [(0.0, start_node)]
        distances: dict[int, float] = {start_node: 0.0}
        previous: dict[int, int | None] = {start_node: None}

        while queue:
            current_distance, current_node = heapq.heappop(queue)
            if current_node == end_node:
                break
            if current_distance > distances.get(current_node, float("inf")):
                continue
            for neighbor, weight in self.index.adjacency.get(current_node, []):
                candidate_distance = current_distance + weight
                if candidate_distance < distances.get(neighbor, float("inf")):
                    distances[neighbor] = candidate_distance
                    previous[neighbor] = current_node
                    heapq.heappush(queue, (candidate_distance, neighbor))

        if end_node not in distances:
            # Disconnected graph — return a fallback route using euclidean distance
            fallback_distance = self.euclidean_distance_m(start_node, end_node)
            start = self.index.nodes[start_node]
            end = self.index.nodes[end_node]
            route = Route(
                start_node=start_node,
                end_node=end_node,
                distance_m=fallback_distance,
                path_nodes=[start_node, end_node],
                coords=[(start.lon, start.lat), (end.lon, end.lat)],
            )
            self._route_cache[cache_key] = route
            return route

        path_nodes = self._reconstruct_path(previous, end_node)
        coords = [
            (self.index.nodes[node_id].lon, self.index.nodes[node_id].lat)
            for node_id in path_nodes
        ]
        route = Route(
            start_node=start_node,
            end_node=end_node,
            distance_m=distances[end_node],
            path_nodes=path_nodes,
            coords=coords,
        )
        self._route_cache[cache_key] = route
        return route

    def distance_matrix(
        self,
        origins: list[int],
        destinations: list[int],
    ) -> dict[tuple[int, int], float]:
        matrix: dict[tuple[int, int], float] = {}
        for origin in origins:
            for destination in destinations:
                matrix[(origin, destination)] = self.shortest_path(origin, destination).distance_m
        return matrix

    def travel_minutes(self, distance_m: float, avg_speed_kmph: float) -> float:
        if avg_speed_kmph <= 0:
            raise ValueError("Average speed must be positive.")
        meters_per_minute = (avg_speed_kmph * 1000.0) / 60.0
        return distance_m / meters_per_minute

    def euclidean_distance_m(self, start_node: int, end_node: int) -> float:
        start = self.index.nodes[start_node]
        end = self.index.nodes[end_node]
        return math.dist((start.lon, start.lat), (end.lon, end.lat)) * 111_000

    @staticmethod
    def _reconstruct_path(previous: dict[int, int | None], end_node: int) -> list[int]:
        path_nodes: list[int] = []
        current: int | None = end_node
        while current is not None:
            path_nodes.append(current)
            current = previous.get(current)
        return list(reversed(path_nodes))

