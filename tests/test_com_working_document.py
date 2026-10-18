"""The COM engine works on *its* drawing, not on whatever tab the operator has in front.

Measured on AutoCAD 2027 on 2026-10-18. A tool sequence drew a gatehouse into a new drawing, and the operator
then had another drawing (a club house) in front. The rest of the batch failed, and `drawing_save_as` saved the
club house under the gatehouse's file name. `_acad_doc()` followed `ActiveDocument` on every call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import backends.com_backend as cb


class FakeDoc:
    def __init__(self, app, name):
        self.Application = app
        self.Name = name
        self.FullName = ""
        self.activated = 0

    def Activate(self):  # noqa: N802 - COM name
        self.activated += 1
        self.Application.ActiveDocument = self


class FakeDocuments(list):
    @property
    def Count(self):  # noqa: N802 - COM name
        return len(self)


@pytest.fixture
def app(monkeypatch):
    a = SimpleNamespace()
    a.Documents = FakeDocuments()
    a.ActiveDocument = None
    monkeypatch.setitem(cb._COM_STATE, "app", a)
    monkeypatch.setattr(cb, "_acad_app", lambda: a)
    yield a
    cb._COM_STATE.pop("doc", None)


def open_doc(app, name):
    d = FakeDoc(app, name)
    app.Documents.append(d)
    app.ActiveDocument = d
    return d


def test_without_a_pin_the_active_drawing_is_used(app):
    a = open_doc(app, "A.dwg")
    assert cb._acad_doc() is a


def test_the_pinned_drawing_survives_a_tab_switch(app):
    gatehouse = open_doc(app, "porteria.dxf")
    cb._pin_document(gatehouse)
    open_doc(app, "club.dxf")  # the operator brings another drawing to the front
    assert app.ActiveDocument.Name == "club.dxf"
    assert cb._acad_doc() is gatehouse


def test_a_closed_pinned_drawing_falls_back_to_the_active_one(app):
    gatehouse = open_doc(app, "porteria.dxf")
    cb._pin_document(gatehouse)
    club = open_doc(app, "club.dxf")
    app.Documents.remove(gatehouse)
    assert cb._acad_doc() is club
    assert "doc" not in cb._COM_STATE


def test_view_and_command_paths_activate_the_working_drawing(app):
    gatehouse = open_doc(app, "porteria.dxf")
    cb._pin_document(gatehouse)
    open_doc(app, "club.dxf")
    assert cb._acad_doc_active() is gatehouse
    assert app.ActiveDocument is gatehouse and gatehouse.activated == 1
    assert (
        cb._acad_doc_active() is gatehouse and gatehouse.activated == 1
    )  # already active: no second switch


def test_a_pin_from_another_application_object_is_dropped(app, monkeypatch):
    gatehouse = open_doc(app, "porteria.dxf")
    cb._pin_document(gatehouse)
    other = SimpleNamespace(Documents=FakeDocuments(), ActiveDocument=None)
    fresh = open_doc(other, "porteria.dxf")
    monkeypatch.setattr(cb, "_acad_app", lambda: other)
    assert cb._acad_doc() is fresh  # a reconnect does not resurrect the old object
    assert "doc" not in cb._COM_STATE


# --- a missing layer is created; AutoCAD's own words reach the error -------------------------------------------

pywintypes = pytest.importorskip("pywintypes")


def _key_not_found():
    return pywintypes.com_error(
        -2147352567,
        "Exception occurred.",
        (0, "AutoCAD.Application", "Key not found", None, -2145386476, -2145386476),
        None,
    )


class FakeEntity:
    def __init__(self, layers):
        self._layers = layers
        self.Document = SimpleNamespace(Layers=SimpleNamespace(Add=lambda name: layers.add(name)))
        self._layer = "0"

    @property
    def Layer(self):  # noqa: N802 - COM name
        return self._layer

    @Layer.setter
    def Layer(self, name):  # noqa: N802 - COM name
        if name not in self._layers:
            raise _key_not_found()
        self._layer = name


def test_a_layer_missing_from_the_drawing_is_created_on_the_entitys_document():
    layers = {"0"}
    e = FakeEntity(layers)
    cb._apply_entity_attrs(e, "TITLEBLOCK", None, None)
    assert e.Layer == "TITLEBLOCK" and "TITLEBLOCK" in layers


def test_the_error_carries_autocads_description():
    assert cb._com_description(_key_not_found()) == "Key not found"
