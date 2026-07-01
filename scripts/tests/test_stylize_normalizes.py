#!/usr/bin/env python3
# scripts/tests/test_stylize_normalizes.py
# stylize_for_publish must always emit a 1080x1350 (4:5) image, whatever size the
# gen backend returns — and raw passthrough must too. No network: the backend and
# overlay-phrase calls are stubbed. Run: python3 scripts/tests/test_stylize_normalizes.py

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

import mandem.stylize as st  # noqa: E402

W45, H45 = 1080, 1350


def _raw(w=500, h=335):
    d = Path(tempfile.mkdtemp())
    p = d / "raw.jpg"
    Image.new("RGB", (w, h), (60, 60, 60)).save(p)
    return p


def _patch(seedream_size, identity_ok=True, seedream_fails=False):
    """Point QUEUE_ROOT at a temp dir; stub Seedream (fal) + overlay + identity check."""
    tmp_queue = Path(tempfile.mkdtemp())
    st.QUEUE_ROOT = tmp_queue

    def fake_seedream(src_path, word, out_path, **k):
        if seedream_fails:
            raise RuntimeError("fal down")
        Image.new("RGB", seedream_size, (90, 90, 90)).save(out_path, "JPEG")
        return Path(out_path)

    st.falimg.seedream_stylise = fake_seedream
    st.make_overlay_phrase = lambda *a, **k: "SCENES"
    st.same_subject = lambda *a, **k: {"same": identity_ok}  # identity gate stub
    return tmp_queue


def test_tall_gen_output_is_normalized_to_4x5():
    _patch((1024, 1536))  # the gpt-image portrait size
    res = st.stylize_for_publish(
        draft_id=1, raw_image_path=_raw(), final_caption="Big scenes at the Emirates.",
        event_summary="Arsenal 4-3 Spurs (Premier League)",
    )
    assert Image.open(res.image_path).size == (W45, H45), Image.open(res.image_path).size


def test_square_gen_output_is_normalized_to_4x5():
    _patch((1024, 1024))
    res = st.stylize_for_publish(
        draft_id=2, raw_image_path=_raw(), final_caption="Cold.",
        event_summary="draft #2",
    )
    assert Image.open(res.image_path).size == (W45, H45), Image.open(res.image_path).size


def test_identity_mutation_falls_back_to_composite():
    # Seedream changed the player → identity gate fails → deterministic composite of
    # the REAL photo, still exactly 4:5, never the mutated AI image.
    _patch((1024, 1536), identity_ok=False)
    res = st.stylize_for_publish(
        draft_id=3, raw_image_path=_raw(), final_caption="TIMELESS\nbig take",
        event_summary="draft #3",
    )
    assert res.backend == "composite_overlay", res.backend
    assert Image.open(res.image_path).size == (W45, H45)


def test_seedream_failure_falls_back_to_composite():
    # fal/Seedream down or refuses → deterministic composite, still a usable 4:5 post.
    _patch((1024, 1536), seedream_fails=True)
    res = st.stylize_for_publish(
        draft_id=4, raw_image_path=_raw(), final_caption="COOKED\nbig take",
        event_summary="draft #4",
    )
    assert res.backend == "composite_overlay", res.backend
    assert Image.open(res.image_path).size == (W45, H45)


def test_headline_color_threads_to_seedream():
    # mix-by-moment: the colour passed to stylize_for_publish reaches the Seedream call.
    tmp = Path(tempfile.mkdtemp())
    st.QUEUE_ROOT = tmp
    captured = {}

    def fake(src, word, out, color="orange", **k):
        captured["color"] = color
        Image.new("RGB", (1080, 1350), (9, 9, 9)).save(out, "JPEG")
        return Path(out)

    st.falimg.seedream_stylise = fake
    st.make_overlay_phrase = lambda *a, **k: "GOAT"
    st.same_subject = lambda *a, **k: {"same": True}
    st.stylize_for_publish(
        draft_id=6, raw_image_path=_raw(), final_caption="GOAT\nlegend stuff",
        event_summary="draft #6", headline_color="gold",
    )
    assert captured.get("color") == "gold", captured


def test_seedream_success_is_used():
    # happy path: Seedream output passes identity check → used as-is, 4:5.
    _patch((1080, 1350), identity_ok=True)
    res = st.stylize_for_publish(
        draft_id=5, raw_image_path=_raw(), final_caption="TIMELESS\nbig take",
        event_summary="draft #5",
    )
    assert res.backend == "seedream", res.backend
    assert Image.open(res.image_path).size == (W45, H45)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
