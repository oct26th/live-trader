"""Shared pytest fixtures."""
import os
import sys

# Put project root on path so tests can import config/indicators/etc directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
