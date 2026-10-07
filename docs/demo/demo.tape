# Record the demo as a GIF with VHS (https://github.com/charmbracelet/vhs):
#
#   vhs docs/demo/demo.tape            # writes docs/demo/keelgate-demo.gif
#
# The pacing lives in demo.sh, so the tape only starts it and waits. Run it from the repo root with
# `keelgate` on PATH (an activated virtualenv with `pip install -e .` is enough).
Output docs/demo/keelgate-demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1200
Set Height 720
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Type "bash docs/demo/demo.sh"
Sleep 400ms
Enter
Sleep 66s
