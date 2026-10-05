"""[RX] line splitting — replays real BBS text through the shared splitter.

Cases 1-3 are the exact bytes behind qtc-debug-20260920-161541.log, which
came out as three [RX] lines each. Everything else guards the prompts that
MUST still appear with no CR behind them.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from transport import _stream_rx_lines


def run(chunks):
    """Feed chunks in order, as separate reads/frames. Returns [RX] lines."""
    out, buf = [], ""
    for c in chunks:
        buf = _stream_rx_lines(c, buf, out.append)
    return [l for l in out if l]


CASES = [
    # name, chunks fed, expected [RX] lines
    ("LinBPQ node: invalid command",
     ["MYNODE:N0CALL-7} Invalid command - Enter ? for command list\r\n"],
     ["MYNODE:N0CALL-7} Invalid command - Enter ? for command list"]),

    ("LinBPQ node: telnet CTEXT",
     ["Welcome to NWI LinBPQ Telnet Server\r\nEnter ? for list of commands\r\n"],
     ["Welcome to NWI LinBPQ Telnet Server", "Enter ? for list of commands"]),

    ("LinBPQ node: command list",
     ["MYNODE:N0CALL-7} BBS CHAT CONNECT BYE INFO NODES PORTS ROUTES USERS MHEARD\r"],
     ["MYNODE:N0CALL-7} BBS CHAT CONNECT BYE INFO NODES PORTS ROUTES USERS MHEARD"]),

    ("Subject line with a colon",
     ["12345 P 1024 N0CALL  Re: tonight's net\r\n"],
     ["12345 P 1024 N0CALL  Re: tonight's net"]),

    # ── Prompts with no CR — these must still flush at once ──────────
    ("Telnet Username prompt",
     ["Username:"], ["Username:"]),
    ("Telnet Username prompt, trailing blank",
     ["Username: "], ["Username:"]),
    ("Telnet Password prompt",
     ["Password:"], ["Password:"]),
    ("BBS prompt after a banner",
     ["[BPQ-6.0.25.36-IHJM$]\rde N0CALL>"],
     ["[BPQ-6.0.25.36-IHJM$]", "de N0CALL>"]),
    ("BBS prompt, trailing blank",
     ["de N0CALL> "], ["de N0CALL>"]),
    ("Node prompt ends on '}' — waits for the rest, as before",
     ["MYNODE:N0CALL-7} "], []),

    # ── Page prompts: the reason the rule exists at all ──────────────
    ("Read pause prompt",
     ["<A>bort, <CR> Continue..>"], ["<A>bort, <CR> Continue..>"]),
    ("Listing page prompt split across two AX.25 frames",
     ["<A>bort, <R Msg(", "s)>, <CR> = Continue..>"],
     ["<A>bort, <R Msg(s)>, <CR> = Continue..>"]),

    # ── Reassembly across reads ──────────────────────────────────────
    ("Sentence split mid-word across two reads",
     ["Enter ? for com", "mand list\r\n"], ["Enter ? for command list"]),
    ("Prompt arriving in the read after its line",
     ["Output aborted\r", "de N0CALL>"], ["Output aborted", "de N0CALL>"]),
    ("Blank lines produce nothing",
     ["\r\n\r\n"], []),
    ("Body text with a URL",
     ["See http://example.com:8080/ for info\r\n"],
     ["See http://example.com:8080/ for info"]),
]

fails = 0
for name, chunks, want in CASES:
    got = run(chunks)
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        want {want}")
        print(f"        got  {got}")

# Known limit, stated not asserted: a read boundary landing exactly after a
# mid-sentence prompt char still splits — same as before the fix, never worse.
edge = run(["MYNODE:N0CALL-7} Invalid command - Enter ?", " for command list\r\n"])
print(f"\nNote — read boundary right after a mid-sentence '?': {edge}")

print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)
