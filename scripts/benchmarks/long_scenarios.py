"""Long browser benchmarks. Each new required field or choice costs one UI action.

A submission records what the browser sent, even if it differs from the goal.
The oracle compares the completed route and its saved records with the expected
choices. Expected answers and test plans are never embedded in the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit

from decision_browser.contracts import Check


@dataclass(frozen=True)
class Field:
    label: str
    expected: str
    options: tuple[str, ...] = ()
    checkbox: bool = False


@dataclass(frozen=True)
class Section:
    heading: str
    prompt: str
    action: str
    fields: tuple[Field, ...] = ()
    alternatives: tuple[str, ...] = ()

    def choices(self) -> tuple[str, ...]:
        return self.alternatives or (self.action,)

    def expected_record(self) -> dict:
        return {"action": self.action, "fields": {f.label: f.expected for f in self.fields}}

    def record_text(self, record: dict) -> str:
        details = ", ".join(f"{key}: {value}" for key, value in record["fields"].items())
        suffix = f" — {details}" if details else ""
        return f"{self.heading}: {record['action']}{suffix}"


@dataclass(frozen=True)
class Workflow:
    title: str
    goal: str
    values: dict[str, str]
    sections: tuple[Section, ...]

    @property
    def minimum_actions(self) -> int:
        # All fields start empty. Toggling an optional feature requested in the
        # goal and committing the section are separate browser interactions.
        return sum(1 + len(section.fields) for section in self.sections)

    def checks(self, url: str) -> list[Check]:
        receipt = "\n".join(section.record_text(section.expected_record()) for section in self.sections)
        return [
            Check(kind="route", expected=url + "summary"),
            Check(kind="text", target="#saved-selections", expected=receipt),
        ]


def menu(label: str, expected: str, *options: str) -> Field:
    return Field(label, expected, options)


def toggle(label: str) -> Field:
    return Field(label, "on", checkbox=True)


def links(heading: str, prompt: str, expected: str, *choices: str) -> Section:
    return Section(heading, prompt, expected, alternatives=choices)


CASES = {
    "rail_itinerary": Workflow(
        "Rail itinerary",
        "Save a rail itinerary from York to Durham. Pick the earliest direct train after 09:00, "
        "choose a Window seat in the Quiet coach, and use that train's listed platform.",
        {"From station": "York", "To station": "Durham"},
        (
            Section("Journey search", "Find departures between two stations.", "Find trains",
                    (Field("From station", "York"), Field("To station", "Durham"))),
            links("Timetable", "Local 09:05 changes at Darlington. Express 09:20 is direct, "
                  "platform 4. Express 09:55 is direct, platform 2.",
                  "Express 09:20", "Local 09:05", "Express 09:20", "Express 09:55"),
            Section("Coach preferences", "Set the preferences for the selected train.", "Apply preferences",
                    (menu("Seat", "Window", "Aisle", "Window"), toggle("Quiet coach"))),
            Section("Station route", "Select the departure platform from the timetable.", "Add platform",
                    (menu("Departure platform", "Platform 4", "Platform 2", "Platform 4", "Platform 6"),)),
            Section("Itinerary review", "Review the journey before saving the local itinerary.", "Save itinerary"),
        ),
    ),
    "library_research": Workflow(
        "Reading list",
        "Find the Glacier Atlas field guide in the Earth Science collection. Follow its appendix "
        "reference to the matching companion work, choose the peer-reviewed edition, and save both "
        "works in a reading list named Ice Notes tagged Fieldwork.",
        {"Catalogue query": "Glacier Atlas", "Reading list name": "Ice Notes"},
        (
            Section("Library catalogue", "Search for material in a collection.", "Search catalogue",
                    (Field("Catalogue query", "Glacier Atlas"),
                     menu("Collection", "Earth Science", "Geography", "Earth Science", "History"))),
            links("Search results", "Choose a source to inspect.", "Glacier Atlas field guide",
                  "Glacier Atlas photo book", "Glacier Atlas field guide", "Glacier Atlas index"),
            Section("Source record", "Use the bibliography in the appendix to locate its companion work.",
                    "Read appendix"),
            Section("Appendix references", "Field guide appendix: the companion is reference GL-42. "
                    "The photo book companion is GL-24; the index companion is GL-14.", "Look up reference",
                    (menu("Companion reference", "GL-42", "GL-14", "GL-24", "GL-42"),)),
            links("Companion editions", "Select an edition to pair with the source.", "Peer-reviewed paper",
                  "Conference abstract", "Peer-reviewed paper", "Working paper"),
            Section("Collection details", "Name and tag the list containing these two works.", "Preview list",
                    (Field("Reading list name", "Ice Notes"), menu("List tag", "Fieldwork", "Teaching", "Fieldwork"))),
            Section("Reading list review", "Review your two works and list details.", "Save reading list"),
        ),
    ),
    "equipment_pickup": Workflow(
        "Equipment pick list",
        "Prepare a local equipment pick list for the Boreal weatherproof camera, with a Telephoto "
        "lens and Protective case. Choose the depot with both items in stock, Friday Afternoon, "
        "for collector Lina Cruz, and save the list.",
        {"Equipment query": "Boreal", "Collector name": "Lina Cruz"},
        (
            Section("Equipment catalogue", "Look up the requested equipment.", "Search equipment",
                    (Field("Equipment query", "Boreal"), menu("Category", "Cameras", "Sensors", "Cameras", "Tripods"))),
            links("Camera records", "Match the weatherproof model.", "Boreal WX weatherproof",
                  "Boreal Studio", "Boreal WX weatherproof", "Boreal Pocket"),
            Section("Accessories", "Configure the camera kit.", "Add accessories",
                    (menu("Lens", "Telephoto", "Wide angle", "Telephoto", "Macro"), toggle("Protective case"))),
            links("Depot inventory", "West has the camera only. East has the camera, telephoto lens "
                  "and case. North lacks a telephoto lens.", "East depot", "West depot", "East depot", "North depot"),
            Section("Pickup window", "Choose the collection day and time.", "Set pickup window",
                    (menu("Pickup day", "Friday", "Thursday", "Friday", "Saturday"),
                     menu("Pickup window", "Afternoon", "Morning", "Afternoon"))),
            Section("Collection contact", "Record who will collect this kit.", "Add collector",
                    (Field("Collector name", "Lina Cruz"),)),
            Section("Kit review", "Review the kit and save the local pick list.", "Save pick list"),
        ),
    ),
    "support_handoff": Workflow(
        "Support handoff draft",
        "Prepare a handoff draft for ticket R-214 in the Network queue. Inspect the activity log "
        "and classify the most recent fault as High priority. Route it to Network Operations and "
        "include the On-call rotation. Add note 'Compare edge counters' tagged Diagnosis and save the draft.",
        {"Ticket number": "R-214", "Handoff note": "Compare edge counters"},
        (
            Section("Support desk", "Find the ticket in its queue.", "Search tickets",
                    (menu("Queue", "Network", "Access", "Network", "Storage"), Field("Ticket number", "R-214"))),
            links("Ticket results", "Open a case record.", "R-214 edge packet loss",
                  "R-241 edge latency", "R-214 edge packet loss", "R-124 switch alarm"),
            Section("Ticket record", "Fault classifications are listed in the activity log.", "Open activity log"),
            Section("Activity log", "09:00 LINK-12 recovered. 10:00 EDGE-28 reported. "
                    "11:00 EDGE-72 reported by the edge monitor.", "Classify fault",
                    (menu("Fault code", "EDGE-72", "LINK-12", "EDGE-28", "EDGE-72"),
                     menu("Priority", "High", "Normal", "High", "Urgent"))),
            Section("Routing", "Set the receiving team and rotation.", "Apply routing",
                    (menu("Receiving team", "Network Operations", "Network Operations", "Storage Operations"),
                     toggle("On-call rotation"))),
            Section("Handoff notes", "Add the requested context to the draft.", "Add note",
                    (Field("Handoff note", "Compare edge counters"),
                     menu("Note tag", "Diagnosis", "History", "Diagnosis", "Resolution"))),
            Section("Ticket workspace", "The classification, routing and note are ready for review.", "Review handoff"),
            Section("Handoff review", "This saves a local draft.", "Save handoff draft"),
        ),
    ),
    "exhibit_planner": Workflow(
        "Exhibit route",
        "Plan a Bristol museum visit in Spring. Start with Tidal Maps, turn on English captions and "
        "Audio guide, then follow its linked wing to the Fresco Study. Choose the earliest slot "
        "after noon with Step-free access. Name the route Harbour Walk, use the Two stops template, "
        "set owner Mira Lane and save the plan.",
        {"Museum city": "Bristol", "Route name": "Harbour Walk", "Plan owner": "Mira Lane"},
        (
            Section("Museum catalogue", "Find exhibitions by city and season.", "Explore exhibitions",
                    (Field("Museum city", "Bristol"), menu("Season", "Spring", "Winter", "Spring", "Summer"))),
            links("First exhibition", "Choose the starting exhibition.", "Tidal Maps",
                  "Tidal Models", "Tidal Maps", "Tidal Poems"),
            Section("First stop guide", "Tidal Maps links to the Copper wing. Tidal Models links to "
                    "the Stone wing and Tidal Poems to the Glass wing.", "Add first stop",
                    (menu("Caption language", "English", "French", "English", "Spanish"), toggle("Audio guide"))),
            links("Wing directory", "Follow the connection listed on the first exhibition guide.", "Copper wing",
                  "Glass wing", "Stone wing", "Copper wing"),
            links("Second exhibition", "Choose an exhibition for the second stop.", "Fresco Study",
                  "Fresco Sketches", "Fresco Study", "Fresco Materials"),
            Section("Visit options", "Choose the time and access requirements.", "Set visit options",
                    (menu("Entry slot", "12:30", "11:45", "12:30", "13:00"), toggle("Step-free access"))),
            Section("Route editor", "Name the route and choose its layout.", "Preview route",
                    (Field("Route name", "Harbour Walk"), menu("Route template", "Two stops", "Single stop", "Two stops"))),
            Section("Route ownership", "Choose who will keep the plan.", "Add owner", (Field("Plan owner", "Mira Lane"),)),
            Section("Exhibit route review", "Save the completed local plan.", "Save exhibit route"),
        ),
    ),
}


def _stage_path(case_id: str, cursor: int) -> str:
    if cursor == len(CASES[case_id].sections):
        return f"/{case_id}/summary"
    return f"/{case_id}/stage/{cursor}" if cursor else f"/{case_id}/"


def _render(case_id: str, records: list[dict], error: str = "") -> str:
    case = CASES[case_id]
    section_no = len(records)
    saved = [f"<li>{escape(section.record_text(record))}</li>" for section, record in zip(case.sections, records)]
    history = '<h2>Saved selections</h2><ol id="saved-selections">' + "\n".join(saved) + "</ol>" if saved else ""
    if section_no == len(case.sections):
        content = f'<p id="status">{escape(case.title)} saved</p>{history}'
    else:
        section = case.sections[section_no]
        parts = [f"<h2>{escape(section.heading)}</h2><p>{escape(section.prompt)}</p>"]
        if error:
            parts.append(f'<p role="alert">{escape(error)}</p>')
        path = _stage_path(case_id, section_no + 1)
        if section.alternatives:
            for i, choice in enumerate(section.choices()):
                parts.append(f'<p><a href="{path}?choice={i}">{escape(choice)}</a></p>')
        else:
            parts.append(f'<form action="{path}" method="post">')
            for i, field in enumerate(section.fields):
                parts.append(f'<p><label for="field-{i}">{escape(field.label)}</label> ')
                if field.checkbox:
                    parts.append(f'<input type="checkbox" id="field-{i}" name="f{i}">')
                elif field.options:
                    opts = "".join(f'<option>{escape(option)}</option>' for option in field.options)
                    parts.append(f'<select required id="field-{i}" name="f{i}">'
                                 f'<option value="">Choose an option</option>{opts}</select>')
                else:
                    parts.append(f'<input required id="field-{i}" name="f{i}" autocomplete="off">')
                parts.append("</p>")
            parts.append(f'<button type="submit">{escape(section.action)}</button></form>')
        content = "".join(parts) + history
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><title>{escape(case.title)}</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<style>body{font:18px system-ui;max-width:800px;margin:28px auto;padding:0 18px}'
            'input,button,select{font:inherit}p{margin:.6em 0}li{margin:.2em 0}</style>'
            f'<body><main><h1>{escape(case.title)}</h1>{content}</main></body></html>')


class ScenarioSite:
    """An isolated HTTP server with authoritative workflow records, one per case."""

    def __init__(self):
        self._records: dict[str, list[dict]] = {}
        self._lock = Lock()
        self._server = None
        self._thread = None

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def page(self, case_id, records, error=""):
                body = _render(case_id, records, error).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def redirect(self, location):
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def handle_section(self, method):
                url = urlsplit(self.path)
                case_id = url.path.strip("/").split("/")[0]
                with owner._lock:
                    if case_id not in owner._records:
                        self.send_error(404)
                        return
                    case = CASES[case_id]
                    records = owner._records[case_id]
                    cursor = len(records)
                    current_url = _stage_path(case_id, cursor)
                    if method == "GET" and url.path == current_url:
                        self.page(case_id, records)
                        return
                    if cursor == len(case.sections) or url.path != _stage_path(case_id, cursor + 1):
                        # Opening a guessed receipt or later stage cannot advance the workflow.
                        self.redirect(current_url)
                        return
                    section = case.sections[cursor]
                    fields = {}
                    if method == "GET" and section.alternatives:
                        try:
                            index = int(parse_qs(url.query).get("choice", [""])[0])
                            if index < 0:
                                raise ValueError
                            action = section.choices()[index]
                        except (ValueError, IndexError):
                            self.send_error(404)
                            return
                    elif method == "POST" and not url.query and not section.alternatives:
                        length = int(self.headers.get("Content-Length", 0))
                        if not 0 <= length <= 8192:
                            self.send_error(400)
                            return
                        form = parse_qs(self.rfile.read(length).decode())
                        for i, field in enumerate(section.fields):
                            value = form.get(f"f{i}", [""])[0]
                            if field.checkbox:
                                value = "on" if value == "on" else "off"
                            elif not value.strip() or (field.options and value not in field.options):
                                self.page(case_id, records, "Complete each required field.")
                                return
                            fields[field.label] = value
                        action = section.action
                    else:
                        self.redirect(current_url)
                        return
                    records.append({"action": action, "fields": fields})
                    self.page(case_id, records)

            def do_GET(self):
                self.handle_section("GET")

            def do_POST(self):
                self.handle_section("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def start(self, case_id: str) -> str:
        with self._lock:
            if self._server is None or case_id not in CASES or case_id in self._records:
                raise ValueError("start a new known workflow on an open fixture")
            self._records[case_id] = []
            return f"http://127.0.0.1:{self._server.server_port}/{case_id}/"

    def reset(self, case_id: str) -> None:
        """Restore the exact initial state at the same URL before a provider retry."""
        with self._lock:
            if case_id not in self._records:
                raise ValueError("start workflow before resetting it")
            self._records[case_id] = []

    def verify(self, case_id: str) -> dict:
        expected = [section.expected_record() for section in CASES[case_id].sections]
        with self._lock:
            observed = list(self._records[case_id])
        return {"success": observed == expected, "expected": expected, "observed": observed}

    def __exit__(self, *_args):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
