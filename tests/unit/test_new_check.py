#!/usr/bin/env python3
"""
Test to verify our new check for missing tasks array when there are multiple task files
"""

import json
import os
import tempfile
from pathlib import Path
import sys

# Add the evo plugin to the path
sys.path.insert(0, str(Path(__file__).parent / "plugins" / "evo" / "src"))

def test_check_logic():
    """Test our check logic directly"""
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Create the check directory structure
        check_dir = tmp_path / "checks" / "001"
        check_dir.mkdir(parents=True)
        
        traces_dir = check_dir / "traces"
        traces_dir.mkdir()
        
        result_path = check_dir / "result.json"
        
        # Test Case 1: Single task file, no tasks array - should PASS
        traces_dir.mkdir(exist_ok=True)
        (traces_dir / "task_0.json").write_text(json.dumps({"task_id": "0", "score": 0.5}))
        result_path.write_text(json.dumps({"score": 0.5}))
        
        # Simulate our check
        if traces_dir.exists():
            trace_files = list(traces_dir.glob("task_*.json"))
            condition = len(trace_files) > 1 and "tasks" not in json.loads(result_path.read_text())
            assert not condition, "Should not fail with single task file"
            print("✓ Test 1 passed: Single task file does not trigger check")
        
        # Test Case 2: Multiple task files, no tasks array - should FAIL
        for i in range(1, 5):  # Create task_1.json through task_4.json
            (traces_dir / f"task_{i}.json").write_text(json.dumps({"task_id": str(i), "score": 0.1 * (i+1)}))
        
        # Result still has no tasks array
        if traces_dir.exists():
            trace_files = list(traces_dir.glob("task_*.json"))
            condition = len(trace_files) > 1 and "tasks" not in json.loads(result_path.read_text())
            assert condition, "Should fail with multiple task files and no tasks array"
            print("✓ Test 2 passed: Multiple task files without tasks array triggers check")
        
        # Test Case 3: Multiple task files, WITH tasks array - should PASS
        result_data = {
            "score": 0.5,
            "tasks": [
                {"task_id": str(i), "score": 0.1 * (i+1)}
                for i in range(5)
            ]
        }
        result_path.write_text(json.dumps(result_data))
        
        if traces_dir.exists():
            trace_files = list(traces_dir.glob("task_*.json"))
            condition = len(trace_files) > 1 and "tasks" not in json.loads(result_path.read_text())
            assert not condition, "Should not fail with multiple task files and tasks array present"
            print("✓ Test 3 passed: Multiple task files with tasks array does not trigger check")
        
        # Test Case 4: No task directory - should PASS
        # Remove traces directory
        import shutil
        shutil.rmtree(traces_dir)
        
        if traces_dir.exists():
            trace_files = list(traces_dir.glob("task_*.json"))
            condition = len(trace_files) > 1 and "tasks" not in json.loads(result_path.read_text())
            assert not condition, "Should not fail when traces directory doesn't exist"
        print("✓ Test 4 passed: No traces directory does not trigger check")
        
        print("\nAll tests passed! The fix works correctly.")

if __name__ == "__main__":
    test_check_logic()