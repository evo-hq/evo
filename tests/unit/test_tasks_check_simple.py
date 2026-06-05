#!/usr/bin/env python3
"""
Simple test to verify the logic of our fix for the tasks array check
"""

import json
import os
import tempfile
from pathlib import Path

def test_tasks_check_logic():
    """Test the logic that checks for missing tasks array when there are task trace files"""
    
    # Create a temporary directory for our test
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Create the directory structure that evo would create for a check
        exp_dir = tmp_path / ".evo" / "run_0000" / "experiments" / "exp_0000" / "checks" / "001"
        exp_dir.mkdir(parents=True)
        
        traces_dir = exp_dir / "traces"
        traces_dir.mkdir()
        
        # Test case 1: No task files - should not trigger our check
        result_file = exp_dir / "result.json"
        result_file.write_text(json.dumps({"score": 0.3}), encoding="utf-8")
        
        trace_files = list(traces_dir.glob("task_*.json"))
        has_tasks_array = "tasks" in json.loads(result_file.read_text()) if result_file.exists() else False
        
        # Should not trigger: 0 trace files > 1 is False
        condition1 = len(trace_files) > 1 and not has_tasks_array
        print(f"Test 1 - No task files: {len(trace_files)} files, has_tasks_array: {has_tasks_array}, trigger: {condition1}")
        assert not condition1, "Should not trigger when there are no task files"
        
        # Test case 2: One task file - should not trigger our check
        task_file = traces_dir / "task_0.json"
        task_file.write_text(json.dumps({
            "task_id": "0",
            "score": 0.3
        }), encoding="utf-8")
        
        trace_files = list(traces_dir.glob("task_*.json"))
        has_tasks_array = "tasks" in json.loads(result_file.read_text()) if result_file.exists() else False
        
        # Should not trigger: 1 trace file > 1 is False
        condition2 = len(trace_files) > 1 and not has_tasks_array
        print(f"Test 2 - One task file: {len(trace_files)} files, has_tasks_array: {has_tasks_array}, trigger: {condition2}")
        assert not condition2, "Should not trigger when there is only one task file"
        
        # Test case 3: Multiple task files, no tasks array - SHOULD trigger our check
        # Create more task files
        for i in range(1, 5):
            task_file = traces_dir / f"task_{i}.json"
            task_file.write_text(json.dumps({
                "task_id": str(i),
                "score": 0.1 * (i + 1)
            }), encoding="utf-8")
        
        # Result file still has no tasks array
        trace_files = list(traces_dir.glob("task_*.json"))
        has_tasks_array = "tasks" in json.loads(result_file.read_text()) if result_file.exists() else False
        
        # SHOULD trigger: 5 trace files > 1 is True and no tasks array is True
        condition3 = len(trace_files) > 1 and not has_tasks_array
        print(f"Test 3 - Multiple task files, no tasks array: {len(trace_files)} files, has_tasks_array: {has_tasks_array}, trigger: {condition3}")
        assert condition3, "Should trigger when there are multiple task files but no tasks array"
        
        # Test case 4: Multiple task files, WITH tasks array - should NOT trigger our check
        # Update result to include tasks array
        result_data = {
            "score": 0.3,
            "tasks": [
                {"task_id": str(i), "score": 0.1 * (i + 1)} 
                for i in range(5)
            ]
        }
        result_file.write_text(json.dumps(result_data), encoding="utf-8")
        
        trace_files = list(traces_dir.glob("task_*.json"))
        has_tasks_array = "tasks" in json.loads(result_file.read_text()) if result_file.exists() else False
        
        # Should NOT trigger: 5 trace files > 1 is True but has_tasks_array is True
        condition4 = len(trace_files) > 1 and not has_tasks_array
        print(f"Test 4 - Multiple task files, with tasks array: {len(trace_files)} files, has_tasks_array: {has_tasks_array}, trigger: {condition4}")
        assert not condition4, "Should not trigger when there are multiple task files and a tasks array"
        
        print("All tests passed!")

if __name__ == "__main__":
    test_tasks_check_logic()