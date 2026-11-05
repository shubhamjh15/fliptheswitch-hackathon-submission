from snitch.switch_client import parse_sse


async def lines(text):
    for line in text.split("\n"):
        yield line


async def test_parses_frames_ids_and_skips_keepalives():
    raw = (
        "event: connection_state\ndata: {\"cursor\": 5}\n\n"
        ": keepalive\n\n"
        "id: 6\nevent: message\ndata: {\"type\":\"message\",\n"
        "data: \"sequence\":6}\n\n"
    )
    frames = [f async for f in parse_sse(lines(raw))]
    assert [f.event for f in frames] == ["connection_state", "message"]
    assert frames[0].data == {"cursor": 5}
    assert frames[1].id == 6 and frames[1].data["sequence"] == 6
