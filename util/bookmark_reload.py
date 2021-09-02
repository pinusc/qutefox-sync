# this file is to be called with
# :debug-pyeval --file FILENAME
# within qutebrowser
bm = objreg.get('bookmark-manager')
bm.marks.clear()
bm._lineparser._read()
for line in bm._lineparser:
    if not line.strip():
        continue
    bm._parse_line(line)


