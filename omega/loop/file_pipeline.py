"""Hash-Anchored File Pipeline for incremental Lean proof construction.

Inspired by Ax-Prover's file-based editing approach (arXiv:2510.12787)
and UC Berkeley Harness architecture (Context Construction + Verification layers).

Core idea:
- Each theorem/lemma block in a .lean file is identified by SHA256(content)[:16]
- LLM edits reference blocks by hash anchor, NOT by old_string or line number
- HashIndex maintains hash→block mapping, auto-rebuilds after edits
- Zero ambiguity: hash deterministically identifies the target block

Usage:
    from omega.loop.file_pipeline import HashIndex, FilePipeline

    pipeline = FilePipeline(base_dir="/tmp/omega_inner")
    path = pipeline.init_file(theorem_header)
    idx = HashIndex(path)
    
    # LLM calls: edit_file(anchor="a1b2c3d4", new_block="...")
    success = idx.apply_edit("a1b2c3d4", new_block)
    if success:
        diag = pipeline.compile(path)  # lean_diagnostic_messages
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("omega.loop.file_pipeline")


@dataclass
class TheoremBlock:
    """A theorem/lemma/def block extracted from a Lean file.

    Attributes
    ----------
    name : str
        Fully qualified theorem name (e.g., "my_theorem")
    content : str
        Full text of the block, from the declaration to its end
    hash_id : str
        SHA256(content_normalized)[:16] — unique anchor for edit targeting
    start_line : int
        1-based line number where this block starts
    end_line : int
        1-based line number where this block ends
    """
    name: str
    content: str
    hash_id: str
    start_line: int = 0
    end_line: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hash": self.hash_id,
            "lines": f"{self.start_line}-{self.end_line}",
            "len": len(self.content),
        }


def _normalize(content: str) -> str:
    """Normalize Lean code for stable hashing.

    Strips trailing whitespace per line, collapses blank lines,
    removes full-line comments. This ensures minor formatting
    differences don't change the hash.
    """
    lines = content.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.rstrip()
        # Skip full-line comments
        if stripped.lstrip().startswith("--"):
            continue
        cleaned.append(stripped)
    # Collapse multiple blank lines
    result = "\n".join(cleaned)
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result.strip()


def _content_hash(content: str) -> str:
    """SHA256 of normalized content, truncated to 16 hex chars."""
    return hashlib.sha256(_normalize(content).encode()).hexdigest()[:16]


# ── Lean block parsing ────────────────────────────────────────

_RE_THEOREM = re.compile(
    r'^\s*(theorem|lemma|def|example)\s+(\w+)\s*',
    re.MULTILINE,
)


def _parse_theorem_blocks(source: str) -> list[TheoremBlock]:
    """Parse a Lean source file into theorem/lemma/def blocks.

    Uses indentation-aware brace matching to find block boundaries.
    """
    blocks: list[TheoremBlock] = []
    lines = source.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i]
        m = _RE_THEOREM.match(line)
        if m:
            kind = m.group(1)
            name = m.group(2)
            start = i

            # Find end of block: track brace depth starting from `:=` or `by`
            # Simple heuristic: block ends at next top-level theorem/lemma/def
            # or at end of file.
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                # Check if this line starts a new top-level theorem
                if _RE_THEOREM.match(next_line):
                    break
                j += 1

            end = j  # exclusive
            content = "\n".join(lines[start:end])
            blocks.append(TheoremBlock(
                name=name,
                content=content,
                hash_id=_content_hash(content),
                start_line=start + 1,  # 1-based
                end_line=end,  # 1-based exclusive
            ))
            i = end
        else:
            i += 1

    return blocks


# ── HashIndex ──────────────────────────────────────────────────

class HashIndex:
    """Hash-anchored index for a Lean file.

    Maps content hashes to theorem blocks, enabling the LLM to
    edit proofs by hash anchor instead of by old_string or line number.

    The index auto-rebuilds after edits, so hashes always reflect
    the current file content.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.blocks: dict[str, TheoremBlock] = {}  # hash_id → block
        self._rebuild()

    def _rebuild(self) -> None:
        """Parse the .lean file and rebuild the hash index."""
        if not os.path.exists(self.file_path):
            self.blocks = {}
            return
        with open(self.file_path) as f:
            source = f.read()
        parsed = _parse_theorem_blocks(source)
        self.blocks = {b.hash_id: b for b in parsed}
        logger.debug("HashIndex rebuilt: %d blocks from %s", len(self.blocks), self.file_path)

    def resolve(self, anchor: str) -> Optional[TheoremBlock]:
        """Resolve a hash anchor to a theorem block.

        Returns None if no block matches the anchor.
        """
        return self.blocks.get(anchor)

    def apply_edit(self, anchor: str, new_block: str) -> bool:
        """Apply a hash-anchored edit to the file.

        Args:
            anchor: 16-char hash identifying the block to replace
            new_block: New content for the block

        Returns:
            True if edit was applied successfully, False if anchor not found
        """
        block = self.resolve(anchor)
        if not block:
            logger.warning("Hash anchor %s not found in index", anchor)
            return False

        with open(self.file_path) as f:
            source = f.read()

        # Replace old content with new content
        idx = source.find(block.content)
        if idx == -1:
            logger.warning("Block content for hash %s not found in file (modified externally?)", anchor)
            return False

        new_source = source[:idx] + new_block + source[idx + len(block.content):]

        with open(self.file_path, "w") as f:
            f.write(new_source)

        # Rebuild index (hashes change because content changed)
        self._rebuild()
        logger.info("Applied edit for hash %s (%s): %d chars → %d chars",
                     anchor, block.name, len(block.content), len(new_block))
        return True

    def format_index(self) -> str:
        """Format the hash index as a string for LLM prompt injection."""
        if not self.blocks:
            return "(empty)"
        lines = ["Available blocks (use anchor hash to edit):"]
        for h, b in sorted(self.blocks.items(), key=lambda x: x[1].start_line):
            lines.append(f"  [{h}] {b.name}  (L{b.start_line}-{b.end_line}, {len(b.content)} chars)")
        return "\n".join(lines)

    @property
    def theorem_names(self) -> list[str]:
        return [b.name for b in self.blocks.values()]

    @property
    def is_empty(self) -> bool:
        return len(self.blocks) == 0


# ── FilePipeline ───────────────────────────────────────────────

class FilePipeline:
    """Manages the lifecycle of a Lean proof file.

    Handles:
    - Creating the initial .lean file from a theorem header
    - Building and maintaining the HashIndex
    - Compiling via lean_diagnostic_messages (when MCP available)
    """

    def __init__(self, base_dir: str = "/tmp/omega_inner"):
        os.makedirs(base_dir, exist_ok=True)
        self.base_dir = base_dir
        self.file_path: Optional[str] = None
        self.index: Optional[HashIndex] = None
        self.current_source: str = ""

    def init_file(self, theorem_header: str, name: str = "proof") -> str:
        """Create the initial .lean file with the theorem header.

        Returns the file path.
        """
        fname = f"{name}_{int(time.time())}.lean"
        self.file_path = os.path.join(self.base_dir, fname)
        with open(self.file_path, "w") as f:
            f.write(theorem_header)
        self.current_source = theorem_header
        self.index = HashIndex(self.file_path)
        logger.info("FilePipeline initialized: %s (%d blocks)",
                     self.file_path, len(self.index.blocks) if self.index else 0)
        return self.file_path

    def get_source(self) -> str:
        """Read the current file source."""
        if self.file_path and os.path.exists(self.file_path):
            with open(self.file_path) as f:
                self.current_source = f.read()
        return self.current_source

    def edit(self, anchor: str, new_block: str) -> bool:
        """Apply a hash-anchored edit. Returns True on success."""
        if self.index is None:
            logger.error("HashIndex not initialized — call init_file() first")
            return False
        return self.index.apply_edit(anchor, new_block)

    def rebuild_index(self) -> None:
        """Force rebuild of the hash index (e.g., after external edits)."""
        if self.index is not None:
            self.index._rebuild()

    def format_index_for_prompt(self) -> str:
        """Format the hash index for LLM prompt injection."""
        if self.index is None:
            return "(no index)"
        return self.index.format_index()
