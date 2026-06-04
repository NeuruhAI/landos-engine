"""LandOS signal normalizers — one module per PUBLIC-records source.

Each source implements the Normalizer interface in base.py. Adding a source is a
new subclass (rows-not-code); the framework handles upsert + audit logging.
"""
