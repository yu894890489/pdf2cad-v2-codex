"""Compatibility entry point for the whole-face correction viewer check.

The review button now applies the user's requested inferred geometry edit.
This check leaves the selected face corrected, verifies undo/redo, and preserves
the native PDF, BOM and upstream observations.
"""
from check_assembly_face_correction import main


if __name__=='__main__':main()
