#!/usr/bin/env python3
"""v3: put the framebuffer inside the kernel's physmap.

DarwinBoot allocates one window, loads the Mach-O into it, and tells the kernel
    Args->PhysicalBase = Address      (load address)
    Args->MemorySize   = WindowSize   (same value)
arm_vm_init builds its physmap as [PhysicalBase, PhysicalBase + MemorySize).

The cepheus framebuffer sits at 0x90000000, *below* the 0x848... region where a
512 MiB window cannot fit, so the screen is never in the physmap and the early
console dies at the first TTBR0 switch ("fb outside physmap; console off").

v3 does two things, in this order:
  1. Try *progressively smaller* windows (requested -> 64 MiB in halves) and
     accept only one placed at or below the framebuffer. Small windows do fit in
     low RAM; a large one cannot.
  2. Widen only the *declared* MemorySize so the linear physmap reaches past the
     framebuffer. Nothing extra is allocated: those pages are simply mapped.

Usage:
  python3 patch_darwinboot.py <path-to>/DarwinBootPkg/Driver/DarwinBoot.c
"""
import io
import sys

SIG_OLD = (
    "AllocateWindow (KERNEL *Kernel, UINT64 WindowSize, EFI_PHYSICAL_ADDRESS *Address)\n"
    "{\n"
)
SIG_NEW = (
    "AllocateWindow (KERNEL *Kernel, UINT64 *WindowSizeP, UINT64 VideoBase, EFI_PHYSICAL_ADDRESS *Address)\n"
    "{\n"
)

DECL_OLD = "  UINT64                RunBytes;\n"
DECL_NEW = ("  UINT64                RunBytes;\n"
            "  UINT64                TrySize;\n"
            "  UINT64                WindowSize;\n")

LOOP_OLD = (
    "  *Address = 0;\n"
    "  Map = NULL;\n"
)
LOOP_NEW = (
    "  *Address = 0;\n"
    "  Map = NULL;\n"
    "  //\n"
    "  // Only a *small* window can be placed below the GOP framebuffer, which is\n"
    "  // what the kernel's physmap must reach. Descend through the sizes and take\n"
    "  // the first that fits there; never fall back to a high address, because\n"
    "  // that is exactly the situation this patch exists to avoid.\n"
    "  //\n"
    "  Status = EFI_OUT_OF_RESOURCES;\n"
    "  for (TrySize = *WindowSizeP; TrySize >= (SIZE_16MB * 4); TrySize >>= 1) {\n"
    "  WindowSize = TrySize;\n"
)

CHECK_OLD = (
    "    if ((Candidate < Descriptor->PhysicalStart) || (End < Candidate) || (End - Candidate < WindowSize)) { continue; }\n"
)
CHECK_NEW = (
    "    if ((Candidate < Descriptor->PhysicalStart) || (End < Candidate) || (End - Candidate < WindowSize)) { continue; }\n"
    "    if ((VideoBase != 0) && (Candidate > VideoBase)) { continue; }\n"
)

CLOSE_OLD = (
    "      }\n"
    "    }\n"
    "\n"
    "  }\n"
    "  FreePool (Map);\n"
    "  return Status;\n"
    "}\n"
)
CLOSE_NEW = (
    "      }\n"
    "    }\n"
    "\n"
    "  }\n"
    "  if (!EFI_ERROR (Status)) { *WindowSizeP = WindowSize; FreePool (Map); return Status; }\n"
    "  }  // TrySize\n"
    "  FreePool (Map);\n"
    "  return Status;\n"
    "}\n"
)

CALL_OLD = "Status = AllocateWindow (&Kernel, WindowSize, &Address);"
CALL_NEW = "Status = AllocateWindow (&Kernel, &WindowSize, Video[0], &Address);"

WIDEN_OLD = (
    "  Args->MemorySize = WindowSize;\n"
    "  Args->MemorySizeActual = WindowSize;\n"
)
WIDEN_NEW = (
    "  //\n"
    "  // Widen the *declared* range so the linear physmap also covers the GOP\n"
    "  // framebuffer above the allocated window. Nothing new is allocated here;\n"
    "  // these pages simply become part of the kernel's physical map, which is\n"
    "  // what the early console needs in order to survive the TTBR0 switch.\n"
    "  //\n"
    "  if ((Video[0] != 0) && (Video[2] != 0) && (Video[4] != 0) &&\n"
    "      (Video[0] <= MAX_UINT64 - Video[2] * Video[4])) {\n"
    "    UINT64 FbEnd = Video[0] + Video[2] * Video[4];\n"
    "    if ((FbEnd > Address) && (FbEnd - Address > WindowSize)) {\n"
    "      Print (L\"DARWINBOOT: window %lx at %lx widened to %lx to cover fb %lx\\n\",\n"
    "             (UINT64)WindowSize, (UINT64)Address, (UINT64)(FbEnd - Address), (UINT64)Video[0]);\n"
    "      WindowSize = FbEnd - Address;\n"
    "    }\n"
    "  }\n"
    "  Args->MemorySize = WindowSize;\n"
    "  Args->MemorySizeActual = WindowSize;\n"
)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    src = io.open(path, encoding="utf-8").read()

    if CALL_NEW in src and WIDEN_NEW in src:
        print("already patched (v3)")
        return 0
    if "AllocateWindow (KERNEL *Kernel, UINT64 WindowSize, UINT64 VideoBase" in src:
        print("v1 applied; revert DarwinBoot.c first")
        return 1
    if "UINT64 *WindowSizeP" in src:
        print("v2 applied; revert DarwinBoot.c first")
        return 1

    for name, old in (("AllocateWindow signature", SIG_OLD), ("declarations", DECL_OLD),
                      ("function head", LOOP_OLD), ("candidate check", CHECK_OLD),
                      ("function tail", CLOSE_OLD), ("call site", CALL_OLD),
                      ("MemorySize assignment", WIDEN_OLD)):
        if old not in src:
            print(f"ANCHOR MISSING: {name}")
            return 1

    src = src.replace(SIG_OLD, SIG_NEW, 1)
    src = src.replace(DECL_OLD, DECL_NEW, 1)
    src = src.replace(LOOP_OLD, LOOP_NEW, 1)
    src = src.replace(CHECK_OLD, CHECK_NEW, 1)
    src = src.replace(CLOSE_OLD, CLOSE_NEW, 1)
    src = src.replace(CALL_OLD, CALL_NEW, 1)
    src = src.replace(WIDEN_OLD, WIDEN_NEW, 1)

    io.open(path, "w", encoding="utf-8", newline="").write(src)
    print(f"patched (v3) {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
