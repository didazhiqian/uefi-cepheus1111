#!/usr/bin/env python3
"""v2: put the framebuffer inside the kernel's physmap, decoupling the memory we
*allocate* from the memory range we *declare* to the kernel.

Background
----------
DarwinBoot loads the Mach-O into a window it allocates with AllocatePages and
then tells the kernel:
    Args->PhysicalBase = Address          (where the kernel was loaded)
    Args->MemorySize   = WindowSize       (same variable -> same value)

arm_vm_init builds its physmap as [PhysicalBase, PhysicalBase + MemorySize).
cepheus' framebuffer (GOP FrameBufferBase) sits *below* the address a 3 GiB /
512 MiB window can be allocated at, so the screen is never mapped and the early
console dies at the first TTBR0 switch:
    "fb outside physmap; console off"

v1 only preferred windows at or below the framebuffer, which cannot work: the
window would have to span the framebuffer, and that range is not free.

v2 therefore
  * allocates a *small* window (halving the request down to 64 MiB) placed at or
    below the framebuffer, so it really does fit in low RAM, and
  * widens only the *declared* MemorySize so the linear physmap covers the
    framebuffer. The extra pages are not allocated; the kernel just maps them.

Usage:
  python3 patch_darwinboot.py <path-to>/DarwinBootPkg/Driver/DarwinBoot.c
Exit 0 = patched or already patched, 1 = anchors not found.
"""
import io
import sys

# ---------------------------------------------------------------- AllocateWindow
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
            "  UINT64                WindowSize;\n"
            "  UINTN                 Pass;\n")

LOOP_OLD = (
    "  *Address = 0;\n"
    "  Map = NULL;\n"
)
LOOP_NEW = (
    "  *Address = 0;\n"
    "  Map = NULL;\n"
    "  //\n"
    "  // The window is what the kernel *occupies*; the range it is told about\n"
    "  // (Args->MemorySize) is widened later to cover the framebuffer. So try\n"
    "  // progressively smaller allocations until one fits at or below the GOP\n"
    "  // framebuffer -- a large window can never fit there.\n"
    "  //\n"
    "  for (TrySize = *WindowSizeP; TrySize >= (SIZE_16MB * 4); TrySize >>= 1) {\n"
    "  WindowSize = TrySize;\n"
    "  for (Pass = 0; Pass < 2; Pass++) {\n"
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
    "  return Status;\n"
    "}\n"
)
CLOSE_NEW = (
    "      }\n"
    "    }\n"
    "\n"
    "  }\n"
    "  if (!EFI_ERROR (Status)) { break; }  // pass 0 placed the window\n"
    "  }  // Pass\n"
    "  if (!EFI_ERROR (Status)) { *WindowSizeP = WindowSize; FreePool (Map); return Status; }\n"
    "  }  // TrySize\n"
    "  FreePool (Map);\n"
    "  return Status;\n"
    "}\n"
)

# ---------------------------------------------------------------- Boot() wiring
CALL_OLD = "Status = AllocateWindow (&Kernel, WindowSize, &Address);"
CALL_NEW = "Status = AllocateWindow (&Kernel, &WindowSize, Video[0], &Address);"

WIDEN_OLD = (
    "  Args->MemorySize = WindowSize;\n"
    "  Args->MemorySizeActual = WindowSize;\n"
)
WIDEN_NEW = (
    "  //\n"
    "  // Extend the *declared* range so the linear physmap also covers the GOP\n"
    "  // framebuffer, which sits above the window we could allocate. Nothing new\n"
    "  // is allocated here: these pages simply become part of the kernel's\n"
    "  // physical map, which is what the early console needs to survive the\n"
    "  // first TTBR0 switch.\n"
    "  //\n"
    "  if ((Video[0] != 0) && (Video[2] != 0) && (Video[4] != 0) &&\n"
    "      (Video[0] <= MAX_UINT64 - Video[2] * Video[4])) {\n"
    "    UINT64 FbEnd = Video[0] + Video[2] * Video[4];\n"
    "    if ((FbEnd > Address) && (FbEnd - Address > WindowSize)) {\n"
    "      Print (L\"DARWINBOOT: widening memory window %lu -> %lu to cover fb %lx\\n\",\n"
    "             (UINT64)WindowSize, (UINT64)(FbEnd - Address), (UINT64)Video[0]);\n"
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
        print("already patched (v2)")
        return 0

    # v1 may already be applied; refuse rather than half-patch.
    if "AllocateWindow (KERNEL *Kernel, UINT64 WindowSize, UINT64 VideoBase" in src:
        print("v1 signature present but v2 call site missing; revert DarwinBoot.c first")
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
    print(f"patched (v2) {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
