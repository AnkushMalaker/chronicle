"""Chronicle wearable client — BLE audio streaming from OMI/Neo pendants.

The tray (``extras/chronicle-tray``) imports this package as a dependency; it
used to reach in via ``sys.path`` injection, which required careful ordering
because this project and vault-sync both had a flat ``main`` module.
"""
