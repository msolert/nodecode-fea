"""Fake 'abaqus job=<name> input=<file> ... interactive' and 'abaqus terminate job=<name>' (tests only).

A run writes <name>.lck while it lasts, progress lines to <name>.sta, then <name>.odb holding a mock:// URL (the
fake odbAccess reads that), and prints Abaqus's last line. An input file containing '*FAIL' ends with an error.
Windows' cmd splits 'job=X' into 'job' 'X', so both forms are accepted.
"""

import os
import sys
import time

VALUED = ("job", "input", "cpus", "memory", "ask_delete", "user", "scratch")


def parse(argv):
    args, i = {}, 0
    while i < len(argv):
        tok = argv[i]
        if "=" in tok:
            k, v = tok.split("=", 1)
            args[k] = v
        elif tok in VALUED and i + 1 < len(argv):
            args[tok] = argv[i + 1]
            i += 1
        else:
            args[tok] = True
        i += 1
    return args


def main():
    args = parse(sys.argv[1:])
    name = args["job"]
    if "terminate" in args:
        open(name + ".terminate", "w").close()
        print("Sent Terminate message to Abaqus job %s" % name)
        return 0
    inp = open(args.get("input", name + ".inp")).read()
    open(name + ".lck", "w").close()
    try:
        seconds = float(os.environ.get("NOCODE_FAKE_JOB_SECONDS", "0.3"))
        with open(name + ".sta", "w") as sta:
            sta.write(" STEP  INC ATT SEVERE EQUIL TOTAL  TOTAL      STEP       INC OF       DOF    IF\n")
            for inc in range(1, 4):
                time.sleep(seconds / 3)
                if os.path.exists(name + ".terminate"):
                    os.remove(name + ".terminate")
                    print("Abaqus/Analysis terminated by a Terminate message")
                    return 1
                sta.write("   1    %d   1     0     1     1  %.3g  %.3g  0.333  \n" % (inc, inc / 3.0, inc / 3.0))
                sta.flush()
        if "*FAIL" in inp:
            print("***ERROR: the fake input asked to fail")
            print("Abaqus/Analysis exited with errors")
            return 1
        with open(name + ".sta", "a") as sta:
            sta.write(" THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n")
        with open(name + ".odb", "w") as odb:
            odb.write(os.environ.get("NOCODE_FAKE_JOB_ODB", "mock://cantilever?nx=4&ny=1&nz=1&frames=3"))
        print("Abaqus JOB %s COMPLETED" % name)
        return 0
    finally:
        os.remove(name + ".lck")


if __name__ == "__main__":
    sys.exit(main())
