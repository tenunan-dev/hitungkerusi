#!/usr/bin/env python3
"""Entity index + mention resolution for the GE16 events database.

The index is assembled from the stores the pipeline already maintains, so an
event in the SQLite database resolves to the SAME entity identity as the
knowledge graph and the vector databases:

  knowledge graph  work/graph/ge16-knowledge-graph.json   node ids + seat roster
  personnel VDB    work/figures/ge16-personnel.json        1,103 persons + aliases
  parties VDB      work/figures/ge16-parties.json          31 parties + aliases/bloc
  canonical DATA (read-only)
      research/derived/master-list-222-parliamentary-seats.csv  P### <-> name <-> state
      research/derived/dun_to_parliament_mapping.json           N.## <-> name <-> P###

Entity ids follow the knowledge-graph node-id convention:
  person:<lowercased name>   party:<lowercased name>   seat:P104   seat:N.58 Lamag
  bloc:PH  state:Perlis  institution:ec   (the last three are event-store only)

Resolvers are intentionally conservative: a mention must be a whole-word match
on a name of two tokens (or a distinctive single token) before it becomes an
entity link, and seat codes only resolve to a DUN when the state is known.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path


# ---------------------------------------------------------------- utilities ---

URL_RE = re.compile(r"https?://\S+")
CODE_FEDERAL_RE = re.compile(r"\bP\.?\s?(\d{3})\b")
CODE_DUN_RE = re.compile(r"\bN\.?\s?(\d{1,2})\b")
#: words that mark a nearby name as a constituency rather than a place
SEAT_CUE_RE = re.compile(
    r"\b(seat|constituency|parliamentary|parlimen|dewan|dun|adun|assemblyman|assemblywoman"
    r"|mp\b|member of parliament|by-?election|vacan\w*|writ|incumbent|redelin\w*|contest\w*"
    r"|candidate|voters|polling)\b",
    re.IGNORECASE,
)

INSTITUTIONS = {
    "institution:ec": ["Election Commission", "Suruhanjaya Pilihan Raya", "EC chairman",
                       "the EC", "EC's", "EC ", "SPR"],
    "institution:dewan-rakyat": ["Dewan Rakyat", "House of Representatives"],
    "institution:dewan-negara": ["Dewan Negara", "Senate"],
    "institution:speaker": ["Speaker Johari", "Dewan Rakyat Speaker", "the Speaker",
                            "Speaker of the Dewan Rakyat"],
    "institution:federal-court": ["Federal Court"],
    "institution:court-of-appeal": ["Court of Appeal"],
    "institution:high-court": ["High Court"],
    "institution:ros": ["Registrar of Societies", "RoS", "ROS"],
    "institution:macc": ["Malaysian Anti-Corruption Commission", "MACC"],
    "institution:agc": ["Attorney General's Chambers", "AGC"],
    "institution:bnm": ["Bank Negara", "BNM"],
    "institution:dosm": ["Department of Statistics", "DOSM"],
    "institution:pardons-board": ["Pardons Board"],
    "institution:ydpa": ["Yang di-Pertuan Agong", "YDPA", "Agong"],
    "institution:parliament": ["Parliament", "Parliamentary"],
    "institution:cabinet": ["Cabinet"],
    "institution:pm-office": ["Prime Minister's Office", "PMO"],
    "institution:melaka-dun": ["Melaka DUN"],
    # pollsters are actors with their own identity in this domain
    "institution:merdeka-center": ["Merdeka Center", "Merdeka Centre"],
    "institution:ilham-centre": ["Ilham Centre", "Ilham Center"],
    "institution:invoke": ["Invoke Malaysia", "Invoke"],
    "institution:iseas": ["ISEAS"],
}

#: display names for institutions whose slug does not title-case well
INSTITUTION_LABELS = {
    "institution:ec": "Election Commission (EC)",
    "institution:iseas": "ISEAS",
    "institution:melaka-dun": "Melaka State Assembly",
}
#: institutions that publish opinion research — scored with role 'pollster'
POLLSTER_IDS = frozenset({
    "institution:merdeka-center", "institution:ilham-centre",
    "institution:invoke", "institution:iseas",
})

STATES = [
    "Johor", "Kedah", "Kelantan", "Melaka", "Negeri Sembilan", "Pahang", "Perak",
    "Perlis", "Penang", "Pulau Pinang", "Sabah", "Sarawak", "Selangor", "Terengganu",
    "Kuala Lumpur", "Labuan", "Putrajaya",
]
STATE_NORMALISE = {
    "Penang": "Pulau Pinang",
    "Kuala Lumpur": "WP Kuala Lumpur",
    "Labuan": "WP Labuan",
    "Putrajaya": "WP Putrajaya",
}

PUBLISHER_BY_DOMAIN = {
    "malaymail.com": ("Malay Mail", "news"),
    "thestar.com.my": ("The Star", "news"),
    "thestar.com": ("The Star", "news"),
    "bernama.com": ("Bernama", "official"),
    "bernama.my": ("Bernama", "official"),
    "nst.com.my": ("New Straits Times", "news"),
    "revamp.nst.com.my": ("New Straits Times", "news"),
    "malaysiakini.com": ("Malaysiakini", "news"),
    "freemalaysiatoday.com": ("Free Malaysia Today", "news"),
    "channelnewsasia.com": ("CNA", "news"),
    "straitstimes.com": ("The Straits Times", "news"),
    "theedgemalaysia.com": ("The Edge Malaysia", "news"),
    "thevibes.com": ("The Vibes", "news"),
    "utusan.com.my": ("Utusan Malaysia", "news"),
    "theborneopost.com": ("The Borneo Post", "news"),
    "dayakdaily.com": ("DayakDaily", "news"),
    "scoop.my": ("Scoop", "news"),
    "thesinardaily.my": ("The Sina Daily", "news"),
    "sinardaily.my": ("Sinar Daily", "news"),
    "newswav.com": ("Newswav", "aggregator"),
    "latestmalaysia.com": ("Latest Malaysia", "aggregator"),
    "malaysianow.com": ("MalaysiaNow", "news"),
    "malaysianindiantoday.com": ("Malaysian Indian Today", "news"),
    "therakyatpost.com": ("The Rakyat Post", "news"),
    "bloomberg.com": ("Bloomberg", "news"),
    "reuters.com": ("Reuters", "news"),
    "scmp.com": ("South China Morning Post", "news"),
    "thediplomat.com": ("The Diplomat", "analysis"),
    "en.wikipedia.org": ("Wikipedia", "reference"),
    "electiondata.my": ("ElectionData.MY", "reference"),
    "merdeka.org": ("Merdeka Center", "official"),
    "ilhamcentre.my": ("Ilham Centre", "official"),
    "dosm.gov.my": ("DOSM", "official"),
    "bnm.gov.my": ("Bank Negara Malaysia", "official"),
    "bernamabiz.com": ("Bernama Biz", "official"),
    "gkg.legal": ("GKG Legal", "analysis"),
    "sabahokay.com": ("Sabah Okay", "news"),
    "themalaysianreserve.com": ("The Malaysian Reserve", "news"),
    "sinarharian.com.my": ("Sinar Harian", "news"),
    "berita.rtm.gov.my": ("RTM", "official"),
}


def publisher_for(url: str) -> tuple[str, str]:
    """(publisher, tier) for a URL, falling back to the bare domain."""
    m = re.match(r"https?://([^/]+)", url)
    domain = (m.group(1) if m else "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    for candidate, value in PUBLISHER_BY_DOMAIN.items():
        if domain == candidate or domain.endswith("." + candidate):
            return value
    return (domain or "unknown", "news")


def url_published_date(url: str) -> str | None:
    """Pull a publication date out of a news URL slug (/2026/08/11/...)."""
    m = re.search(r"/(20\d\d)/(\d{1,2})/(\d{1,2})/", url)
    if m:
        year, month, day = (int(x) for x in m.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    m = re.search(r"(20\d\d)-(\d{1,2})-(\d{1,2})", url)
    if m:
        year, month, day = (int(x) for x in m.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return out.strip("-")


def mask_urls(text: str) -> str:
    """Replace URLs with same-length spaces so offsets and dates stay aligned."""
    return URL_RE.sub(lambda m: " " * len(m.group(0)), text)


# ------------------------------------------------------------- entity index ---

class EntityIndex:
    def __init__(self, repo_root: Path, kg_path: Path | None = None):
        self.repo_root = Path(repo_root)
        self.kg_path = kg_path or (self.repo_root / "work" / "graph" / "ge16-knowledge-graph.json")
        self.entities: dict[str, dict] = {}
        # matcher structures
        self._person_keys: list[tuple[str, str]] = []   # (normalised needle, entity_id)
        self._party_keys: list[tuple[str, str]] = []
        self._institution_keys: list[tuple[str, str]] = []
        self._state_keys: list[tuple[str, str]] = []
        self._seat_by_code: dict[str, str] = {}         # P104 -> seat:P104
        self._seat_by_name: dict[str, str] = {}         # normalised constituency -> seat id
        self._dun_by_code: dict[tuple[str, str], str] = {}   # (state, N.58) -> seat id
        self._dun_by_code_name: dict[tuple[str, str], str] = {}  # (state_lower, "N.58 lamag") -> id
        self._kg_nodes: dict[str, dict] = {}
        self.warnings: list[str] = []

    # ------------------------------------------------------------- internal --
    def _add(self, entity_id, entity_type, name, kg_node_id=None, aliases=(), attrs=None):
        record = self.entities.get(entity_id)
        if record is None:
            record = {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "name": name,
                "kg_node_id": kg_node_id,
                "aliases": [],
                "attrs": attrs or {},
                "first_event_date": None,
                "last_event_date": None,
                "event_count": 0,
            }
            self.entities[entity_id] = record
        for alias in aliases:
            if alias and alias not in record["aliases"]:
                record["aliases"].append(alias)
        if kg_node_id and not record["kg_node_id"]:
            record["kg_node_id"] = kg_node_id
        if attrs:
            merged = dict(record["attrs"])
            merged.update({key: value for key, value in attrs.items() if value not in (None, "")})
            record["attrs"] = merged
        if name and record["name"].upper() == record["name"] and name != name.upper():
            record["name"] = name        # prefer a canonical label over an ALL-CAPS graph label
        return record

    def _register_person(self, name: str, aliases=()):
        if not name or len(name) < 4:
            return None
        entity_id = f"person:{name.lower()}"
        kg_id = entity_id if entity_id in self._kg_nodes else None
        self._add(entity_id, "person", name, kg_id, aliases)
        needles = {name, *aliases}
        for needle in needles:
            key = needle.strip().lower()
            if not key:
                continue
            if len(needle.strip()) < 5 and " " not in needle:
                continue
            self._person_keys.append((key, entity_id))
        return entity_id

    def _register_party(self, name: str, bloc: str | None, aliases=()):
        if not name:
            return None
        entity_id = f"party:{name.lower()}"
        kg_id = entity_id if entity_id in self._kg_nodes else None
        attrs = {"bloc": bloc} if bloc else {}
        self._add(entity_id, "party", name, kg_id, aliases, attrs)
        needle_set = {name, *aliases}
        for needle in needle_set:
            key = needle.strip().lower()
            if len(key) < 3:
                continue
            self._party_keys.append((key, entity_id))
        if bloc:
            bloc_id = f"bloc:{bloc}"
            self._add(bloc_id, "bloc", bloc, None if bloc_id not in self._kg_nodes else bloc_id,
                      [], {"members": []})
        return entity_id

    def _kg_lookup(self, *candidates: str) -> str | None:
        for candidate in candidates:
            if candidate and candidate in self._kg_nodes:
                return candidate
        return None

    # --------------------------------------------------------------- loaders --
    def load_knowledge_graph(self):
        if not self.kg_path.exists():
            self.warnings.append(f"knowledge graph missing: {self.kg_path}")
            return
        graph = json.loads(self.kg_path.read_text(encoding="utf-8"))
        self._kg_nodes = graph.get("nodes", {})
        for node_id, node in self._kg_nodes.items():
            ntype = node.get("type")
            if ntype == "person":
                self._add(node_id, "person", node.get("name", node_id.split(":", 1)[1]),
                          node_id, [], {"cluster": node.get("cluster", "")})
                key = node.get("name", "").lower()
                if len(key) >= 5:
                    self._person_keys.append((key, node_id))
            elif ntype == "party":
                self._add(node_id, "party", node.get("name", ""), node_id, [],
                          {"bloc": node.get("bloc", "")})
            elif ntype == "seat":
                self._add(node_id, "seat", node.get("name", ""), node_id,
                          [], {"kind": node.get("kind", ""), "state": node.get("state", "")})
                name = node.get("name", "")
                m = CODE_FEDERAL_RE.match(name)
                if m:
                    self._seat_by_code[f"P{int(m.group(1)):03d}"] = node_id
                m = re.match(r"N\.?(\d{1,2})\s+(.*)", name)
                if m:
                    code = f"N.{int(m.group(1)):02d}"
                    state = (node.get("state") or "").strip().lower()
                    if state:
                        # DUN codes repeat in every state: a code is only usable as a
                        # lookup key when the state is known, otherwise "N.58" would
                        # resolve to whichever state was indexed last.
                        self._dun_by_code[(state, code)] = node_id
                    self._dun_by_code_name[(state, f"{code} {m.group(2)}".lower())] = node_id

    def load_personnel(self, path: Path):
        if not path.exists():
            self.warnings.append(f"personnel VDB source missing: {path}")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for person in data.get("persons", []):
            entity_id = self._register_person(person.get("name", ""), person.get("aliases", []))
            if not entity_id:
                continue
            parties = sorted({r.get("party", "") for r in person.get("roles", []) if r.get("party")})
            if parties:
                self.entities[entity_id]["attrs"]["parties"] = parties
            seats = sorted({r.get("seat", "") for r in person.get("roles", []) if r.get("seat")})
            if seats:
                self.entities[entity_id]["attrs"]["seats"] = seats

    def load_parties(self, path: Path):
        if not path.exists():
            self.warnings.append(f"party VDB source missing: {path}")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for party in data.get("parties", []):
            self._register_party(party.get("name", ""), party.get("bloc"),
                                 party.get("aliases", []))

    def load_canonical_seats(self, master_csv: Path, dun_mapping: Path):
        """1_DATA is read-only canonical input: federal seats + DUN mapping."""
        if master_csv.exists():
            with master_csv.open(encoding="utf-8") as handle:
                name_votes: dict[str, set] = {}
                for row in csv.DictReader(handle):
                    code = (row.get("code") or "").strip()
                    if not re.fullmatch(r"P\d{3}", code):
                        continue
                    state = (row.get("state") or "").strip()
                    entity_id = f"seat:{code}"
                    self._add(entity_id, "seat", f"{code} {row.get('constituency', '')}".strip(),
                              self._kg_lookup(entity_id),
                              [], {"kind": "federal", "state": state})
                    self._seat_by_code[code] = entity_id
                    name_votes.setdefault((row.get("constituency") or "").strip().lower(), set()).add(entity_id)
                for name, ids in name_votes.items():
                    if name and len(name) > 3 and len(ids) == 1:   # ambiguous names dropped
                        self._seat_by_name[name] = next(iter(ids))
        else:
            self.warnings.append(f"canonical master list missing: {master_csv}")

        if dun_mapping.exists():
            mapping = json.loads(dun_mapping.read_text(encoding="utf-8"))
            parliament_to_state = {}
            for row in mapping:
                pcode = row.get("parliament", "")
                pname = row.get("parliament_name", "")
                if pcode:
                    parliament_to_state[pcode] = self.entities.get(f"seat:{pcode}", {}).get(
                        "attrs", {}).get("state", "")
                code = row.get("dun", "")
                dun_name = row.get("dun_name", "")
                if not re.fullmatch(r"N\.\d{1,2}", code or ""):
                    continue
                state = parliament_to_state.get(pcode, "")
                if not state:
                    # fall back: try to locate any registered federal seat by name
                    state = self.entities.get(self._seat_by_name.get(pname.lower(), ""), {}).get(
                        "attrs", {}).get("state", "")
                canonical_id = f"seat:{code} {dun_name}".strip()
                kg_id = self._kg_lookup(canonical_id, f"seat:{code} {dun_name.upper()}")
                # reuse the graph node id as the entity id so seat identity is shared
                entity_id = kg_id or canonical_id
                self._add(entity_id, "seat", canonical_id.split(":", 1)[1], kg_id,
                          [], {"kind": "dun", "state": state, "federal": pcode})
                if state:
                    self._dun_by_code[(state.lower(), code)] = entity_id
                    self._dun_by_code_name[(state.lower(), f"{code} {dun_name}".lower())] = entity_id
        else:
            self.warnings.append(f"DUN mapping missing: {dun_mapping}")

    def load_institutions_and_states(self):
        for entity_id, needles in INSTITUTIONS.items():
            label = entity_id.split(":", 1)[1].replace("-", " ")
            self._add(entity_id, "institution", INSTITUTION_LABELS.get(entity_id, label.title()))
            for needle in needles:
                self._institution_keys.append((needle.lower(), entity_id))
        for state in STATES:
            entity_id = f"state:{STATE_NORMALISE.get(state, state)}"
            self._add(entity_id, "state", state)
            self._state_keys.append((state.lower(), entity_id))

    # ------------------------------------------------------------- resolving --
    def _match_needles(self, haystack: str, needles: list[tuple[str, str]]) -> list[tuple[int, int, str]]:
        """Whole-word matches of registered needles: (start, length, entity_id)."""
        hits: list[tuple[int, int, str]] = []
        for needle, entity_id in needles:
            start = 0
            while True:
                idx = haystack.find(needle, start)
                if idx < 0:
                    break
                before = haystack[idx - 1] if idx else " "
                after = haystack[idx + len(needle)] if idx + len(needle) < len(haystack) else " "
                if not before.isalnum() and not after.isalnum():
                    hits.append((idx, len(needle), entity_id))
                start = idx + len(needle)
        # longest match wins; one entity never takes two overlapping spans
        hits.sort(key=lambda h: (-h[1], h[0]))
        chosen: list[tuple[int, int, str]] = []
        seen_entities: set[str] = set()
        occupied: list[tuple[int, int]] = []
        for start, length, entity_id in hits:
            if entity_id in seen_entities:
                continue
            end = start + length
            if any(start < o_end and o_start < end for o_start, o_end in occupied):
                continue
            chosen.append((start, length, entity_id))
            seen_entities.add(entity_id)
            occupied.append((start, end))
        return chosen

    def resolve(self, text: str, state: str | None = None) -> list[dict]:
        """Entity mentions in `text` as [{entity_id, role, mention, position}]."""
        if not text:
            return []
        masked = mask_urls(text)
        haystack = masked.lower()
        rows: list[dict] = []

        def push(hits, role):
            for start, length, entity_id in hits:
                rows.append({"entity_id": entity_id, "role": role,
                             "mention": masked[start:start + length].strip(), "position": start})

        push(self._match_needles(haystack, self._person_keys), "actor")
        push(self._match_needles(haystack, self._party_keys), "actor")
        push(self._match_needles(haystack, self._bloc_keys()), "bloc")
        push(self._match_needles(haystack, self._institution_keys), "institution")
        push(self._match_needles(haystack, self._state_keys), "state")

        resolved_seats: dict[str, int] = {}
        for match in CODE_FEDERAL_RE.finditer(masked):
            code = f"P{int(match.group(1)):03d}"
            entity_id = self._seat_by_code.get(code)
            if entity_id:
                resolved_seats[entity_id] = match.start()
        for name, entity_id in self._seat_by_name.items():
            idx = haystack.find(name)
            if idx < 0 or entity_id in resolved_seats:
                continue
            # A constituency named without its code is often just a place name
            # ("Kota Kinabalu", "Kangar"): require an electoral cue nearby.
            window = haystack[max(0, idx - 34):idx + len(name) + 34]
            if not SEAT_CUE_RE.search(window):
                continue
            resolved_seats[entity_id] = idx
        for match in CODE_DUN_RE.finditer(masked):
            code = f"N.{int(match.group(1)):02d}"
            key = ((state or "").lower(), code)
            entity_id = self._dun_by_code.get(key)
            if entity_id and entity_id not in resolved_seats:
                resolved_seats[entity_id] = match.start()
        for (key_state, key), entity_id in self._dun_by_code_name.items():
            if state and key_state != state.lower():
                continue
            # the mapping says 'n.13 guar sanji'; prose writes 'Guar Sanji (N.13)'
            alternate = re.sub(r"^(n\.\d+)\s+(.*)$", r"\2 (\1", key)
            found = haystack.find(key)
            if found < 0 and alternate != key:
                found = haystack.find(alternate)
            if found >= 0 and entity_id not in resolved_seats:
                resolved_seats[entity_id] = found
        for entity_id, position in resolved_seats.items():
            rows.append({"entity_id": entity_id, "role": "seat",
                         "mention": self.entities[entity_id]["name"], "position": position})

        # one row per (event, entity, role)
        deduped: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["entity_id"], row["role"])
            if key not in deduped or row["position"] < deduped[key]["position"]:
                deduped[key] = row
        return sorted(deduped.values(), key=lambda r: r["position"])

    def _bloc_keys(self) -> list[tuple[str, str]]:
        if getattr(self, "_bloc_cache", None) is None:
            keys: list[tuple[str, str]] = []
            for entity_id, record in self.entities.items():
                if record["entity_type"] != "bloc":
                    continue
                keys.append((record["name"].lower(), entity_id))
            # coalition shorthand that must not fire on substrings
            for shorthand, entity_id in (
                ("pakatan harapan", "bloc:PH"), ("perikatan nasional", "bloc:PN"),
                ("barisan nasional", "bloc:BN"),
            ):
                if entity_id in self.entities:
                    keys.append((shorthand, entity_id))
            self._bloc_cache = keys
        return self._bloc_cache

    def stats(self) -> dict:
        by_type: dict[str, int] = {}
        for record in self.entities.values():
            by_type[record["entity_type"]] = by_type.get(record["entity_type"], 0) + 1
        return {
            "entities": len(self.entities),
            "by_type": by_type,
            "person_needles": len(self._person_keys),
            "party_needles": len(self._party_keys),
            "federal_seats_by_code": len(self._seat_by_code),
            "federal_seats_by_name": len(self._seat_by_name),
            "dun_seats": len({v for v in self._dun_by_code_name.values()}),
        }


def build_index(repo_root: Path, kg_path: Path | None = None, figures_dir: Path | None = None,
                data_root: Path | None = None) -> EntityIndex:
    """Assemble the full entity index from the sibling stores."""
    repo_root = Path(repo_root)
    figures_dir = Path(figures_dir) if figures_dir else repo_root / "work" / "figures"
    data_root = Path(data_root) if data_root else repo_root.parent / "1_DATA"

    index = EntityIndex(repo_root, kg_path=kg_path)
    index.load_knowledge_graph()
    index.load_institutions_and_states()
    index.load_personnel(figures_dir / "ge16-personnel.json")
    index.load_parties(figures_dir / "ge16-parties.json")
    index.load_canonical_seats(
        data_root / "research" / "derived" / "master-list-222-parliamentary-seats.csv",
        data_root / "research" / "derived" / "dun_to_parliament_mapping.json",
    )
    return index
