import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The pipeline modules read config at import time.
TEST_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.test.yaml")
os.environ["ROOTVIZ_CONFIG"] = TEST_CONFIG
