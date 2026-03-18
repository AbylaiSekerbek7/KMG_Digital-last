from uto_routing.graph import RoadGraph
from uto_routing.sample_data import create_sample_dataset


def test_shortest_path_returns_valid_route() -> None:
    dataset = create_sample_dataset()
    graph = RoadGraph.from_dataset(dataset)

    route = graph.shortest_path(1, 6)

    assert route.start_node == 1
    assert route.end_node == 6
    assert route.distance_m > 0
    assert route.path_nodes[0] == 1
    assert route.path_nodes[-1] == 6
    assert len(route.coords) == len(route.path_nodes)

