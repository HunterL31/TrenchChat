"""
The tile encoder and the coalescing store, against scripted frames.

A scripted frame sequence stands in for a monitor (tests/fake_audio.py does
the same for a microphone): only the tiles that changed are encoded, a frame
that changed almost everywhere goes out as one image, the share never exceeds
its size ceiling, and a consumer that missed several updates gets one that
covers all of them.
"""

from PIL import Image, ImageDraw

from trenchchat.core.screen.capture import ScriptedSource
from trenchchat.core.screen.encoder import (
    Consumer, PRESET_CLEARER, PRESET_SMOOTHER, TileEncoder, TileStore,
    next_update, preset_settings, share_size,
)
from trenchchat.network.screen_wire import (
    KIND_FULL, KIND_TILES, MAX_SHARE_HEIGHT, MAX_SHARE_WIDTH,
    check_update_images, pack_update, unpack_update,
)


def desktop(width: int = 400, height: int = 300, colour=(30, 30, 40)) -> Image.Image:
    return Image.new("RGB", (width, height), colour)


def with_box(frame: Image.Image, box, colour=(220, 40, 40)) -> Image.Image:
    edited = frame.copy()
    ImageDraw.Draw(edited).rectangle(box, fill=colour)
    return edited


class TestEncoder:
    def test_the_first_frame_is_a_full_image(self):
        encoder = TileEncoder(400, 300)
        update = encoder.encode(desktop())
        assert update is not None and update.kind == KIND_FULL
        assert (update.width, update.height) == (400, 300)
        check_update_images(update)

    def test_an_unchanged_frame_costs_nothing(self):
        encoder = TileEncoder(400, 300)
        encoder.encode(desktop())
        assert encoder.encode(desktop()) is None
        assert encoder.updates_out == 1

    def test_only_the_tiles_that_changed_are_encoded(self):
        encoder = TileEncoder(400, 300)
        encoder.encode(desktop())
        update = encoder.encode(with_box(desktop(), (10, 10, 40, 40)))
        assert update is not None and update.kind == KIND_TILES
        assert [(tx, ty) for tx, ty, _d in update.entries] == [(0, 0)]
        check_update_images(update)

    def test_a_change_across_two_tiles_names_both(self):
        encoder = TileEncoder(400, 300)
        encoder.encode(desktop())
        update = encoder.encode(with_box(desktop(), (120, 10, 140, 40)))
        assert sorted((tx, ty) for tx, ty, _d in update.entries) == [(0, 0), (1, 0)]

    def test_an_edge_tile_declares_its_remainder(self):
        encoder = TileEncoder(300, 200)
        encoder.encode(desktop(300, 200))
        update = encoder.encode(with_box(desktop(300, 200), (290, 190, 299, 199)))
        assert [(tx, ty) for tx, ty, _d in update.entries] == [(2, 1)]
        check_update_images(update)
        assert unpack_update(pack_update(update)).entries == update.entries

    def test_a_frame_that_changed_almost_everywhere_is_one_image(self):
        encoder = TileEncoder(400, 300)
        encoder.encode(desktop())
        update = encoder.encode(desktop(colour=(200, 200, 200)))
        assert update.kind == KIND_FULL and len(update.entries) == 1

    def test_the_share_is_scaled_to_the_ceiling_and_never_up(self):
        assert share_size(3840, 2160, MAX_SHARE_WIDTH, MAX_SHARE_HEIGHT) == (1920, 1080)
        assert share_size(800, 600, MAX_SHARE_WIDTH, MAX_SHARE_HEIGHT) == (800, 600)
        assert share_size(2560, 1080, 1280, 720) == (1280, 540)
        encoder = TileEncoder(3840, 2160)
        update = encoder.encode(desktop(3840, 2160))
        assert (update.width, update.height) == (1920, 1080)
        check_update_images(update)

    def test_the_sequence_counts_updates_not_frames(self):
        encoder = TileEncoder(400, 300)
        first = encoder.encode(desktop())
        encoder.encode(desktop())
        second = encoder.encode(with_box(desktop(), (0, 0, 5, 5)))
        assert (first.seq, second.seq) == (1, 2)
        assert encoder.stats()["frames_in"] == 3

    def test_presets(self):
        assert preset_settings(PRESET_SMOOTHER)["max_height"] == 720
        assert preset_settings(PRESET_CLEARER)["fps"] == 15
        assert preset_settings("nonsense")["max_width"] == MAX_SHARE_WIDTH
        assert preset_settings(PRESET_CLEARER, fps=90)["fps"] == 30
        assert preset_settings(PRESET_CLEARER, fps=0)["fps"] == 1


class TestStoreAndConsumer:
    def _sequence(self):
        encoder = TileEncoder(400, 300)
        frames = [desktop(), with_box(desktop(), (10, 10, 40, 40)),
                  with_box(desktop(), (10, 10, 40, 40), (0, 200, 0)),
                  with_box(with_box(desktop(), (10, 10, 40, 40), (0, 200, 0)),
                           (300, 200, 350, 250))]
        return [u for u in (encoder.encode(f) for f in frames) if u is not None]

    def test_a_consumer_kept_up_to_date_gets_each_update(self):
        store, consumer = TileStore(), Consumer()
        sent = []
        for update in self._sequence():
            store.apply(update)
            consumer.note(update)
            sent.append(next_update(store, consumer))
        assert [u.kind for u in sent] == [KIND_FULL, KIND_TILES, KIND_TILES, KIND_TILES]
        assert not consumer.behind

    def test_a_consumer_that_missed_everything_gets_one_full_then_one_tile_update(self):
        store, consumer = TileStore(), Consumer()
        for update in self._sequence():
            store.apply(update)
            consumer.note(update)
        first = next_update(store, consumer)
        assert first.kind == KIND_FULL
        second = next_update(store, consumer)
        assert second.kind == KIND_TILES
        assert sorted((tx, ty) for tx, ty, _d in second.entries) == [(0, 0), (2, 1)]
        assert next_update(store, consumer) is None

    def test_a_missed_tile_carries_its_latest_bytes_not_its_first(self):
        store, consumer = TileStore(), Consumer()
        updates = self._sequence()
        store.apply(updates[0])
        consumer.note(updates[0])
        next_update(store, consumer)
        for update in updates[1:3]:
            store.apply(update)
            consumer.note(update)
        coalesced = next_update(store, consumer)
        assert len(coalesced.entries) == 1
        assert coalesced.entries[0][2] == updates[2].entries[0][2]

    def test_a_new_full_frame_clears_stale_tiles(self):
        store = TileStore()
        encoder = TileEncoder(400, 300)
        store.apply(encoder.encode(desktop()))
        store.apply(encoder.encode(with_box(desktop(), (0, 0, 20, 20))))
        assert store.tiles
        store.apply(encoder.encode(desktop(colour=(255, 255, 255))))
        assert not store.tiles and store.full is not None

    def test_a_store_holds_one_frame_however_long_the_share_runs(self):
        store, consumer = TileStore(), Consumer()
        encoder = TileEncoder(400, 300)
        frame = desktop()
        for step in range(40):
            frame = with_box(frame, (step * 5 % 380, 0, step * 5 % 380 + 10, 10),
                             (step * 6 % 255, 0, 0))
            update = encoder.encode(frame)
            if update is not None:
                store.apply(update)
                consumer.note(update)
        assert len(store.tiles) <= encoder.cols * encoder.rows
        assert len(consumer.dirty) <= encoder.cols * encoder.rows

    def test_a_size_change_resets_the_store(self):
        store = TileStore()
        store.apply(TileEncoder(400, 300).encode(desktop()))
        store.apply(TileEncoder(200, 100).encode(desktop(200, 100)))
        assert (store.width, store.height) == (200, 100)
        assert store.full is not None and not store.tiles


class TestScriptedSource:
    def test_a_list_repeats_its_last_frame(self):
        source = ScriptedSource([desktop(), desktop(colour=(1, 1, 1))])
        assert source.open() == (400, 300)
        frames = [source.grab() for _ in range(3)]
        assert frames[1] is frames[2]
        assert source.grabs == 3

    def test_a_callable_is_asked_for_each_frame(self):
        calls = []

        def make():
            calls.append(1)
            return desktop()

        source = ScriptedSource(make)
        source.open()
        source.grab()
        source.grab()
        assert len(calls) == 2
