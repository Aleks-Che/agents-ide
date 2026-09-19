"""Loop display boundaries include nested branches, not surrounding stages."""

from agents_ide.engine.loops import loop_body_nodes


def test_nested_loop_bodies_keep_their_own_boundaries():
    outer = {"from": "finish", "to": "first", "loop": {"id": "outer"}}
    inner = {"from": "check", "to": "repair", "loop": {"id": "inner"}}
    graph = {
        "edges": [
            {"from_node": "start", "to_node": "first"},
            {"from": "first", "to": "review"},
            {"from": "review", "to": "check"},
            {"from": "repair", "to": "review"},
            {"from": "check", "to": "finish"},
            {"from": "check", "to": "alternate"},
            {"from": "alternate", "to": "finish"},
            {"from": "finish", "to": "end"},
            outer,
            inner,
        ]
    }
    assert loop_body_nodes(graph, outer) == {
        "first",
        "review",
        "check",
        "repair",
        "alternate",
        "finish",
    }
    assert loop_body_nodes(graph, inner) == {"repair", "review", "check"}


def test_self_loop_only_resets_its_own_stage():
    loop = {"source": "work", "target": "work", "loop": {"id": "retry"}}
    graph = {
        "edges": [
            {"source": "start", "target": "work"},
            {"source": "work", "target": "end"},
            loop,
        ]
    }
    assert loop_body_nodes(graph, loop) == {"work"}
