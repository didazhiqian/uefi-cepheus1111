#!/usr/bin/env python3
"""Make DarwinBoot load the XNU kernel window at or below the GOP framebuffer.

Why: the kernel builds its physmap as [load_address, load_address + memSize).
A framebuffer *below* the load address therefore sits outside the physmap, and
the first TTBR0 switch unmaps it -- the early console dies exactly when the
kernel reaches arm_vm_init ("fb outside physmap; console off"). Keeping the
kernel window at or below the framebuffer puts the screen inside the physmap,
so the console survives into pmap_bootstrap and beyond.

Edit is intentionally string-based: the build pins a DarwinBoot submodule commit
that may move, and a stale unified diff would silently fail to apply.

Usage:
  python3 patch_darwinboot.py <path-to>/DarwinBootPkg/Driver/DarwinBoot.c
Exit code 0 = patched (or already patched), 1 = anchors not found.
"""
import io
import sys

SIG_OLD = (
    "AllocateWindow (KERNEL *Kernel, UINT64 WindowSize, EFI_PHYSICAL_ADDRESS *Address)\n"
    "{\n"
)
SIG_NEW = (
    "AllocateWindow (KERNEL *Kernel, UINT64 WindowSize, UINT64 VideoBase, EFI_PHYSICAL_ADDRESS *Address)\n"
    "{\n"
)

DECL_OLD = "  UINT64                RunBytes;\n"
DECL_NEW = "  UINT64                RunBytes;\n  UINTN                 Pass;\n"

LOOP_OLD = (
    "  for (Offset = 0; Offset + Stride <= Size; Offset += Stride) {\n"
    "    Descriptor = (EFI_MEMORY_DESCRIPTOR *)((UINT8 *)Map + Offset);\n"
    "    if ((Descriptor->Type != EfiConventionalMemory) || (Descriptor->NumberOfPages == 0)) { continue; }\n"
)
LOOP_NEW = (
    "  //\n"
    "  // Two passes: the first only accepts a window at or below the GOP\n"
    "  // framebuffer, so the screen stays inside the kernel's physmap. If that\n"
    "  // finds nothing, the second pass accepts any free run (old behaviour).\n"
    "  //\n"
    "  for (Pass = 0; Pass < 2; Pass++) {\n"
    "  for (Offset = 0; Offset + Stride <= Size; Offset += Stride) {\n"
    "    Descriptor = (EFI_MEMORY_DESCRIPTOR *)((UINT8 *)Map + Offset);\n"
    "    if ((Descriptor->Type != EfiConventionalMemory) || (Descriptor->NumberOfPages == 0)) { continue; }\n"
)

CHECK_OLD = (
    "    if ((Candidate < Descriptor->PhysicalStart) || (End < Candidate) || (End - Candidate < WindowSize)) { continue; }\n"
)
CHECK_NEW = (
    "    if ((Candidate < Descriptor->PhysicalStart) || (End < Candidate) || (End - Candidate < WindowSize)) { continue; }\n"
    "    if ((Pass == 0) && (VideoBase != 0) && (Candidate > VideoBase)) { continue; }\n"
)

CLOSE_OLD = (
    "      }\n"
    "    }\n"
    "\n"
    "  }\n"
    "  FreePool (Map);\n"
)
CLOSE_NEW = (
    "      }\n"
    "    }\n"
    "\n"
    "  }\n"
    "  }  // Pass\n"
    "  FreePool (Map);\n"
)

CALL_OLD = "AllocateWindow (&Kernel, WindowSize, &Address)"
CALL_NEW = "AllocateWindow (&Kernel, WindowSize, Video[0], &Address)"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    src = io.open(path, encoding="utf-8").read()

    if CALL_NEW in src:
        print("already patched")
        return 0

    for name, old in (("signature", SIG_OLD), ("declaration", DECL_OLD), ("loop head", LOOP_OLD),
                      ("candidate check", CHECK_OLD), ("loop close", CLOSE_OLD), ("call site", CALL_OLD)):
        if old not in src:
            print(f"ANCHOR MISSING: {name}")
            return 1

    src = src.replace(SIG_OLD, SIG_NEW, 1)
    src = src.replace(DECL_OLD, DECL_NEW, 1)
    src = src.replace(LOOP_OLD, LOOP_NEW, 1)
    src = src.replace(CHECK_OLD, CHECK_NEW, 1)
    src = src.replace(CLOSE_OLD, CLOSE_NEW, 1)
    src = src.replace(CALL_OLD, CALL_NEW, 1)

    io.open(path, "w", encoding="utf-8", newline="").write(src)
    print(f"patched {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
