"""Deterministic SYNTHETIC benchmark: corpus + questions + gold evidence, generated from a seed.

Every fact is generated first and the documents are rendered from the facts, so gold labels are correct *by construction*
and verified programmatically (`verify`). They are NOT human annotations. The corpus is small and template-based, which makes
it a controlled testbed for comparing retrieval strategies, not a proxy for real enterprise documents."""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field

DOMAINS = {
    "logistics": ["freight routing", "warehouse throughput", "last-mile delivery", "pallet consolidation", "carrier scheduling"],
    "genomics": ["sequence alignment", "variant calling", "gene expression profiling", "assembly pipelines", "sample barcoding"],
    "energy": ["grid balancing", "battery dispatch", "load forecasting", "substation telemetry", "demand response"],
    "finance": ["ledger reconciliation", "credit exposure", "settlement netting", "audit sampling", "liquidity forecasting"],
    "robotics": ["motion planning", "gripper calibration", "sensor fusion", "fleet coordination", "collision avoidance"],
    "healthcare": ["triage scheduling", "bed occupancy", "claims adjudication", "referral routing", "clinical coding"],
    "aerospace": ["thermal modelling", "avionics testing", "orbit determination", "fuel budgeting", "payload integration"],
    "retail": ["assortment planning", "price elasticity", "shelf replenishment", "returns processing", "loyalty segmentation"],
}
CITIES = ["Lisbon", "Osaka", "Nairobi", "Gdansk", "Valparaiso", "Tbilisi", "Hobart", "Reykjavik", "Chennai", "Montreal", "Tallinn", "Dakar"]
FIRST = ["Mara", "Idris", "Selin", "Tomas", "Wen", "Priya", "Oskar", "Lucia", "Kofi", "Anya", "Hugo", "Noor", "Emeka", "Sofia", "Ravi", "Elin"]
LAST = ["Okonkwo", "Varga", "Lindqvist", "Haddad", "Moreau", "Tanaka", "Brennan", "Castellanos", "Adeyemi", "Kowalski", "Raman", "Sato"]
SYL = ["zor", "van", "ta", "pel", "mor", "quix", "ben", "dra", "kel", "ul", "fer", "nox", "ari", "bel", "cor", "dun", "esk", "gal", "hin", "jur", "lum", "myr", "ond", "pyr", "ras", "sil", "tev", "wex", "yal", "zen"]
FILLER = [
    "The programme follows the organisation's standard governance cadence with quarterly reviews.",
    "Status reports are circulated to stakeholders and archived in the shared repository.",
    "Risks are tracked in a register and escalated when they exceed the agreed tolerance.",
    "Procurement for the work package follows the framework agreement already in force.",
    "Training material was updated and a hand-over session was held with the operations team.",
]


@dataclass(slots=True)
class Project:
    name: str
    code: str
    domain: str
    lead: str
    city: str
    year: int
    budget: int
    capability: str
    depends_on: str | None = None


@dataclass(slots=True)
class Question:
    id: str
    qclass: str                 # lookup | factoid | comparison | multi_hop | exploratory | unanswerable
    text: str
    gold_docs: list[str]        # filenames
    answers: list[str] = field(default_factory=list)   # strings that a correct answer must contain
    split: str = "test"         # dev (30%, tuning allowed) | test (70%, report only)


def _name(rng: random.Random, used: set[str]) -> str:
    while True:
        n = "".join(rng.choice(SYL) for _ in range(rng.choice((2, 3)))).capitalize()
        if n not in used and len(n) >= 5:
            used.add(n)
            return n


def build_facts(seed: int = 7, per_domain: int = 3) -> list[Project]:
    rng = random.Random(seed)
    used: set[str] = set()
    codes: set[str] = set()
    projects: list[Project] = []
    for domain, caps in DOMAINS.items():
        for i in range(per_domain):
            while True:
                code = f"{rng.choice('QXZKWV')}{rng.choice('QXZKWV')}-{rng.randint(1000, 9999)}"
                if code not in codes:
                    codes.add(code)
                    break
            projects.append(Project(_name(rng, used), code, domain, f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choice(CITIES),
                                    rng.randint(2016, 2025), rng.randint(2, 98), caps[i % len(caps)]))
    for p in projects:   # one dependency per project, always on a project from a different domain
        p.depends_on = rng.choice([q.name for q in projects if q.domain != p.domain])
    # keep leads unique so "who leads X" has one answer and multi-hop answers are unambiguous
    seen: dict[str, int] = {}
    for p in projects:
        seen[p.lead] = seen.get(p.lead, 0) + 1
        if seen[p.lead] > 1:
            p.lead = f"{rng.choice(FIRST)} {rng.choice(LAST)}-{seen[p.lead]}"
    assert len({p.lead for p in projects}) == len(projects)
    return projects


def render(p: Project, rng: random.Random) -> str:
    paras = [
        f"Project {p.name} overview. Project {p.name} is a {p.domain} initiative focused on {p.capability}. "
        f"Its audit reference code is {p.code}.",
        f"Leadership. Project {p.name} is led by {p.lead}, who reports to the {p.domain} steering group.",
        f"Location and timing. The {p.name} team is based in {p.city}. Project {p.name} was launched in {p.year}.",
        f"Funding. The approved budget for Project {p.name} is {p.budget} million credits.",
        f"Dependencies. Project {p.name} depends on the outputs of Project {p.depends_on}.",
    ]
    fill = rng.sample(FILLER, 3)
    # domain vocabulary shared across the three same-domain projects acts as a realistic distractor
    shared = f"General {p.domain} practice: teams working on {p.capability} coordinate with other {p.domain} programmes."
    return "\n\n".join(paras[:2] + [fill[0]] + paras[2:4] + [shared, fill[1]] + paras[4:] + [fill[2]])


def build_corpus(seed: int = 7) -> tuple[list[Project], dict[str, str]]:
    projects = build_facts(seed)
    rng = random.Random(seed + 1)
    return projects, {f"{p.name.lower()}.txt": render(p, rng) for p in projects}


def _fn(p: Project) -> str:
    return f"{p.name.lower()}.txt"


def build_questions(projects: list[Project], seed: int = 7) -> list[Question]:
    rng = random.Random(seed + 2)
    by = {p.name: p for p in projects}
    qs: list[Question] = []

    def add(qclass, text, gold, answers):
        qs.append(Question(f"q{len(qs) + 1:03d}", qclass, text, gold, answers))

    pool = projects[:]
    rng.shuffle(pool)
    for p in pool[:22]:
        add("lookup", f"audit reference {p.code}", [_fn(p)], [p.code])
    facts = [
        (lambda p: f"What city is the team of Project {p.name} based in?", lambda p: p.city),
        (lambda p: f"Who leads Project {p.name}?", lambda p: p.lead),
        (lambda p: f"In which year was Project {p.name} launched?", lambda p: str(p.year)),
        (lambda p: f"What is the approved budget of Project {p.name}?", lambda p: f"{p.budget} million"),
    ]
    rng.shuffle(pool)
    for i, p in enumerate(pool[:22]):
        qf, af = facts[i % len(facts)]
        add("factoid", qf(p), [_fn(p)], [af(p)])
    rng.shuffle(pool)
    for i in range(22):
        a, b = pool[i], pool[(i + 7) % len(pool)]
        if a is b:
            b = pool[(i + 8) % len(pool)]
        add("comparison", f"Compare the approved budget of Project {a.name} and Project {b.name}.", [_fn(a), _fn(b)],
            [f"{a.budget} million", f"{b.budget} million"])
    rng.shuffle(pool)
    for p in pool[:22]:
        dep = by[p.depends_on]
        add("multi_hop", f"Who leads the project that Project {p.name} depends on, and what city is that team based in?",
            [_fn(p), _fn(dep)], [dep.lead, dep.city])
    doms = list(DOMAINS)
    templates = ["Give an overview of the {d} projects and what they work on.", "Summarise the {d} programme landscape.",
                 "What are all the {d} initiatives about?"]
    combos = [(t, d) for t in templates for d in doms]     # 24 unique (template, domain) pairs; first 22 used
    for t, d in combos[:22]:
        add("exploratory", t.format(d=d), [_fn(p) for p in projects if p.domain == d], [])
    ghost = ["Xandrel", "Quorvath", "Pelinko", "Mubrask", "Tyvenor", "Ulmarth", "Bexiron", "Calvoss", "Dorquin", "Eskovar", "Fyrmund", "Gelbrix"]
    for g in ghost:
        add("unanswerable", rng.choice(["Who leads Project {g}?", "What is the approved budget of Project {g}?"]).format(g=g), [], [])
    for q in qs:   # stable 70/30 split independent of ordering
        q.split = "dev" if int(hashlib.sha256(q.id.encode()).hexdigest(), 16) % 10 < 3 else "test"
    return qs


def verify(projects: list[Project], docs: dict[str, str], qs: list[Question]) -> list[str]:
    """Programmatic gold check. Returns a list of problems (empty == consistent)."""
    problems = []
    for q in qs:
        for g in q.gold_docs:
            if g not in docs:
                problems.append(f"{q.id}: gold doc {g} missing")
        if q.qclass in ("lookup", "factoid", "multi_hop", "comparison"):
            text = " ".join(docs[g] for g in q.gold_docs if g in docs)
            for a in q.answers:
                if a not in text:
                    problems.append(f"{q.id}: answer {a!r} not found in gold docs")
        if q.qclass in ("factoid", "comparison", "multi_hop"):   # every named project must be the *subject* of a gold document
            names = re.findall(r"Project ([A-Z][a-z]+)", q.text)
            for n in names:
                if not any(f.startswith(n.lower() + ".") for f in q.gold_docs):
                    problems.append(f"{q.id}: project {n} named in the question has no gold document")
        if q.qclass == "lookup":
            holders = [f for f, t in docs.items() if q.answers[0] in t]
            if holders != q.gold_docs:
                problems.append(f"{q.id}: code appears in {holders}, expected {q.gold_docs}")
        if q.qclass == "unanswerable":
            ent = q.text.split("Project ")[1].split(" ")[0].rstrip("?")
            if any(ent in t for t in docs.values()):
                problems.append(f"{q.id}: entity {ent} unexpectedly present in corpus")
    if len({q.text for q in qs}) != len(qs):
        problems.append("duplicate question text")
    return problems
