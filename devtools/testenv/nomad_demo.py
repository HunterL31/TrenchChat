"""Seed a tester with a browsable Nomad Network demo node.

Gives every fresh dev environment something to browse in the NET tab with
no manual provisioning: each tester serves a small micron site that names
the tester it belongs to, and hosting stays enabled through the config so
restarts re-announce it. Opt-in via TC_TESTENV_NOMAD_DEMO so the scenario
suite's testers stay unhosted unless a scenario asks.
"""

from trenchchat.core import actions

_INDEX_MU = """\
`c
>{tester}'s demo node
`a
This node is served by `!{tester}`! (identity `F0a0{identity}`f)
over a real Reticulum link, written in micron — Nomad Network's markup.

-

>>Styling

Micron does `!bold`!, `*italic`*, `_underline`_ and colors:
`Ff44red`f, `F0a0green`f, `F09fblue`f, and a `B333`Ff90highlight`f`b.

>>Pages on this node

`[About this node`:/page/about.mu]
`[Mesh art`:/page/art.mu]

-~

`cEdit the files under this tester's nomad_pages directory and RESCAN to
`cchange what it serves.
`a
"""

_ABOUT_MU = """\
>About {tester}'s node

You are reading a page hosted by `!{tester}`!, identity:

`F0a0{identity}`f

Anything in this tester's nomad_pages directory is served to the mesh —
pages/ as `F0a0/page/...`f and files/ as `F0a0/file/...`f. Real NomadNet
and Sideband clients can browse it too; the node speaks the standard
nomadnetwork.node protocol.

-

`[Back to the index`:/page/index.mu]
"""

_ART_MU = """\
>Mesh art, courtesy of {tester}

`=
      {tester} ──── hub ──── you
         │                    │
      [pages]             [browser]
         │                    │
         └── real RNS link ───┘

  `!this is not bold`! and `[this is not a link]
`=

Literal mode above; live markup again `F90fhere`f.

-

`[Back to the index`:/page/index.mu]
"""


def demo_pages(tester_name: str, identity_hex: str) -> dict[str, str]:
    """The demo site's page files, keyed by filename."""
    fields = {"tester": tester_name, "identity": identity_hex}
    return {
        "index.mu": _INDEX_MU.format(**fields),
        "about.mu": _ABOUT_MU.format(**fields),
        "art.mu": _ART_MU.format(**fields),
    }


def seed_demo_node(backend, tester_name: str) -> None:
    """Write the demo pages into a tester's pages directory and enable
    hosting under a name that says whose node it is."""
    pages_dir = backend.node_browser.pages_root / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in demo_pages(tester_name,
                                        backend.identity.hash_hex).items():
        (pages_dir / filename).write_text(content, encoding="utf-8")
    actions.set_node_hosting(backend.node_browser, enabled=True,
                             node_name=f"{tester_name}'s demo node")
