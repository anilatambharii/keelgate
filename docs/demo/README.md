# The 60-second demo

`demo.sh` is the script behind the GIF at the top of the README. Every command in it is real and
runs offline with no API key; nothing is mocked or faked up for the camera.

| Time | What the viewer sees | Command |
|---|---|---|
| 0:00 | The one idea | (title) |
| 0:03 | Install | `pip install keelgate` (shown, not run) |
| 0:08 | Allowed, denied, held for a human | `keelgate quickstart` |
| 0:20 | Edit one audit record and the chain fails | `keelgate quickstart --tamper` |
| 0:32 | A budget stop, a restart and a resume that never repeats the order | `python examples/research_loop.py` |
| 0:45 | 36 red-team cases, all blocked | `keelgate eval run --suite redteam,unit,trajectory` |
| 0:58 | The repo | (closing card) |

## Record it

From the repository root, with Keelgate installed in the active environment
(`pip install -e .`, so `keelgate` is on `PATH`):

```bash
# a GIF, using VHS (https://github.com/charmbracelet/vhs)
vhs docs/demo/demo.tape                 # writes docs/demo/keelgate-demo.gif

# or a terminal recording, using asciinema, then convert with agg
asciinema rec -c "bash docs/demo/demo.sh" docs/demo/keelgate-demo.cast
agg docs/demo/keelgate-demo.cast docs/demo/keelgate-demo.gif
```

The pacing is all in `demo.sh` (`PACE=1` is the recording speed; `PACE=0` removes every pause, which
is how the test suite checks that the script still works). To lengthen or shorten a beat, change the
`pause` values there.

Then embed it in the README:

Add an image line to the README that points at `docs/demo/keelgate-demo.gif`.

!!! note "Not committed"
    The GIF itself is a binary recorded on a developer's machine, so it is not generated or checked
    in by CI. Only the script that produces it is, so it cannot drift from what the software does.
