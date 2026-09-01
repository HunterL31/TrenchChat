"""Seed a tester with a browsable Nomad Network demo node.

Gives every fresh dev environment something to browse in the NET tab with
no manual provisioning: each tester serves a small micron site that names
the tester it belongs to and exercises the awkward parts of the markup --
every link form, anchors, input fields, a table, literal blocks,
background-coloured bars, page colour headers and a partial. Hosting stays
enabled through the config so restarts re-announce it. Opt-in via
TC_TESTENV_NOMAD_DEMO so the scenario suite's testers stay unhosted unless a
scenario asks.
"""

from trenchchat.core import actions
from trenchchat.core.node_browser import nomad_node_hash_for_identity

_INDEX_MU = """\
`c
>{tester}'s demo node
`a`:top
This node is served by `!{tester}`! (identity `F0a0{identity}`f)
over a real Reticulum link, written in micron — Nomad Network's markup.

-

>>Styling

Micron does `!bold`!, `*italic`*, `_underline`_ and colors:
`Ff44red`f, `F0a0green`f, `F09fblue`f, and a `B333`Ff90highlight`f`b.

A bar drawn the way micron draws bars, out of coloured spaces:
`B100 `B300 `B500 `B700 `B900 `Bb00 `Bd00 `Bf00`b

>>Pages on this node

`[About this node`:/page/about.mu]
`[Mesh art`:/page/art.mu]
`[Input fields`:/page/fields.mu]
`[A table`:/page/table.mu]
`[Partials and colours`:/page/live.mu]

>>Every link form

Absolute: `[the index of this node`{node}:/page/index.mu]
Scheme:   `[the same page, nnn@ form`nnn@{node}:/page/index.mu]
Bare:     `[{node}:/page/index.mu]
Anchor:   `[jump to the bottom`#the-bottom]
Cross:    `[open the table page at its heading`:/page/table.mu`anchor=a-table]
Next:     `[continue to the next heading`#]
File:     `[a file this node serves`:/file/notes.txt]
Missing:  `[a page nobody serves`:/page/nowhere.mu]

-~

>The bottom
`:the-bottom
You reached the bottom.

`[Back to the top`#top]
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
`[Back to the index, opened at its bottom`:/page/index.mu`anchor=the-bottom]
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

_FIELDS_MU = """\
>Input fields

A static node cannot answer with the submitted values — only an executable
node-side page can — so the submit links below fetch a plain page. What
they prove is that the fields hold what you typed and that the link carries
it.

>>Text

Name:     `B444`<24|user_name`{tester} fan>`b

Empty:    `B444`<demo_empty`>`b

Masked:   `B444`<!16|passphrase`hunter2>`b

>>Checkboxes

`B444`<?|sign_up|1`>`b Sign me up

`B444`<?|digest|1|*`>`b Send me the digest (pre-checked)

>>Radio group

`B900`<^|color|Red`>`b  Red

`B090`<^|color|Green`>`b Green

`B009`<^|color|Blue`>`b Blue

>>Submit

`[Everything on the page`:/page/echo.mu`*]
`[Only the name`:/page/echo.mu`user_name]
`[The name and a variable`:/page/echo.mu`user_name|action=view]

-

`[Back to the index`:/page/index.mu]
"""

_ECHO_MU = """\
>Echo

This page is a plain file, so it answers the same way whatever you sent.
On a node running an executable page, the submitted fields would arrive as
`F0a0field_`f environment variables and the link's variables as
`F0a0var_`f ones.

-

`[Back to the fields`:/page/fields.mu]
`[Back to the index`:/page/index.mu]
"""

_TABLE_MU = """\
>A table

`tc70
| Page | What it exercises | Size |
| ---- | :---------------: | ---: |
| `[index`:/page/index.mu] | link forms, anchors, bars | small |
| `[fields`:/page/fields.mu] | text, checkbox, radio | `!small`! |
| `[art`:/page/art.mu] | literal blocks | tiny |
| `[live`:/page/live.mu] | partials, page colours | small |
`t

-

`[Back to the index`:/page/index.mu]
"""

_LIVE_MU = """\
#!bg=101418
#!fg=cde
>Partials and colours

This page sets its own colours with the `F0a0#!bg=`f and `F0a0#!fg=`f
headers, so it reads the same here as it does in NomadNet.

Below is a `!partial`!: a second page fetched on its own and dropped in
where the tag sits. This one names itself `F0a0side`f, so the link under
it can reload just that block without reloading the page.

-

`{{:/page/side.mu`0`pid=side}}

-

`[Reload the partial`p:side]
`[Back to the index`:/page/index.mu]
"""

_SIDE_MU = """\
`B333`F0f0 loaded from /page/side.mu `f`b

A partial is an ordinary page. Nothing marks it as one -- the tag in the
page that includes it decides where it lands and how often it is refetched.
"""

_NOTES_TXT = """\
A small file served over the mesh by a TrenchChat node.
Fetching it proves /file/ requests work end to end.
"""


def demo_pages(tester_name: str, identity_hex: str) -> dict[str, str]:
    """The demo site's page files, keyed by filename."""
    fields = {
        "tester": tester_name,
        "identity": identity_hex,
        "node": nomad_node_hash_for_identity(identity_hex),
    }
    return {
        "index.mu": _INDEX_MU.format(**fields),
        "about.mu": _ABOUT_MU.format(**fields),
        "art.mu": _ART_MU.format(**fields),
        "fields.mu": _FIELDS_MU.format(**fields),
        "echo.mu": _ECHO_MU.format(**fields),
        "table.mu": _TABLE_MU.format(**fields),
        "live.mu": _LIVE_MU.format(**fields),
        "side.mu": _SIDE_MU.format(**fields),
    }


def demo_files() -> dict[str, str]:
    """The demo site's downloadable files, keyed by filename."""
    return {"notes.txt": _NOTES_TXT}


def seed_demo_node(backend, tester_name: str) -> None:
    """Write the demo pages into a tester's pages directory and enable
    hosting under a name that says whose node it is."""
    pages_dir = backend.node_browser.pages_root / "pages"
    files_dir = backend.node_browser.pages_root / "files"
    pages_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in demo_pages(tester_name,
                                        backend.identity.hash_hex).items():
        (pages_dir / filename).write_text(content, encoding="utf-8")
    for filename, content in demo_files().items():
        (files_dir / filename).write_text(content, encoding="utf-8")
    actions.set_node_hosting(backend.node_browser, enabled=True,
                             node_name=f"{tester_name}'s demo node")
