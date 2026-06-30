#!/usr/bin/env python3
import os
import re
import sys

def parse_log(log_path):
    if not os.path.exists(log_path):
        print(f"Error: Log file '{log_path}' does not exist.")
        sys.exit(1)
        
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
        
    results = []
    task_re = re.compile(r"\[\*\] Processing Claw-Lite Task \d+/\d+:\s*(\S+)")
    stage1_re = re.compile(r"\[\*\] --- Stage 1: Running WITH RECON")
    stage2_re = re.compile(r"\[\*\] --- Stage 2: Running WITHOUT RECON")
    test_re = re.compile(r"-> Test suite run completed \(Result:\s*(.*?)\)")
    success_re = re.compile(
        r"\[\+\] Successfully benchmarked task\s*(\S+)(?:\s*\|\s*Recon tokens:\s*in=(\d+),\s*out=(\d+)\s*\|\s*Baseline tokens:\s*in=(\d+),\s*out=(\d+))?"
    )
    failed_re = re.compile(r"\[!\] Benchmark execution failed:\s*(.*)")
    loop_err_re = re.compile(r"-> Loop encountered error:\s*(.*)")
    
    current_task = None
    stage1_status = None
    stage2_status = None
    current_stage = None
    
    for line in lines:
        task_match = task_re.search(line)
        if task_match:
            current_task = task_match.group(1)
            stage1_status = None
            stage2_status = None
            current_stage = None
            continue
            
        if not current_task:
            continue
            
        if stage1_re.search(line):
            current_stage = 1
            continue
        elif stage2_re.search(line):
            current_stage = 2
            continue
            
        loop_err_match = loop_err_re.search(line)
        if loop_err_match:
            err_msg = loop_err_match.group(1).strip()
            if current_stage == 1:
                stage1_status = f"Error: {err_msg}"
            elif current_stage == 2:
                stage2_status = f"Error: {err_msg}"
            continue
            
        test_match = test_re.search(line)
        if test_match:
            status = test_match.group(1).strip()
            if current_stage == 1:
                stage1_status = status
            elif current_stage == 2:
                stage2_status = status
            continue
            
        success_match = success_re.search(line)
        if success_match and success_match.group(1) == current_task:
            recon_pass = (stage1_status == "Passed")
            base_pass = (stage2_status == "Passed")
            recon_runnable = (stage1_status != "Unrunnable" and not (stage1_status and stage1_status.startswith("Error:")))
            base_runnable = (stage2_status != "Unrunnable" and not (stage2_status and stage2_status.startswith("Error:")))
            
            recon_in = int(success_match.group(2)) if success_match.group(2) else 0
            recon_out = int(success_match.group(3)) if success_match.group(3) else 0
            base_in = int(success_match.group(4)) if success_match.group(4) else 0
            base_out = int(success_match.group(5)) if success_match.group(5) else 0
            
            results.append({
                "instance_id": current_task,
                "success": True,
                "recon_in": recon_in,
                "recon_out": recon_out,
                "base_in": base_in,
                "base_out": base_out,
                "recon_pass": recon_pass,
                "base_pass": base_pass,
                "runnable": recon_runnable and base_runnable,
                "error": None
            })
            current_task = None
            continue
            
        failed_match = failed_re.search(line)
        if failed_match:
            err_msg = failed_match.group(1).strip()
            results.append({
                "instance_id": current_task,
                "success": False,
                "recon_in": 0,
                "recon_out": 0,
                "base_in": 0,
                "base_out": 0,
                "recon_pass": False,
                "base_pass": False,
                "runnable": False,
                "error": err_msg
            })
            current_task = None
            continue
            
    return results

def print_summary(results, log_name):
    total_runs = len(results)
    successful_runs = [r for r in results if r["success"]]
    total_successful = len(successful_runs)
    
    if total_successful == 0:
        print(f"\n--- Summary for {log_name} ---\nNo successful runs.")
        return

    consistent_count = 0
    runnable_count = 0
    discrepancy_details = []
    
    for r in successful_runs:
        if not r.get("runnable", True):
            continue
        runnable_count += 1
        if r["recon_pass"] == r["base_pass"]:
            consistent_count += 1
        else:
            discrepancy_details.append(f"- `{r['instance_id']}`: Recon pass={r['recon_pass']} | Baseline pass={r['base_pass']}")
 
    consistency_rate = (consistent_count / max(1, runnable_count)) * 100

    sum_recon_in = sum(r["recon_in"] for r in successful_runs)
    sum_recon_out = sum(r["recon_out"] for r in successful_runs)
    sum_base_in = sum(r["base_in"] for r in successful_runs)
    sum_base_out = sum(r["base_out"] for r in successful_runs)

    avg_recon_in = int(sum_recon_in / total_successful)
    avg_recon_out = int(sum_recon_out / total_successful)
    avg_base_in = int(sum_base_in / total_successful)
    avg_base_out = int(sum_base_out / total_successful)

    avg_recon_total = avg_recon_in + avg_recon_out
    avg_base_total = avg_base_in + avg_base_out

    in_savings = f"{(1 - avg_recon_in / max(1, avg_base_in)) * 100:.1f}%" if avg_base_in else "0.0%"
    out_savings = f"{(1 - avg_recon_out / max(1, avg_base_out)) * 100:.1f}%" if avg_base_out else "0.0%"
    total_savings = f"{(1 - avg_recon_total / max(1, avg_base_total)) * 100:.1f}%" if avg_base_total else "0.0%"

    print(f"\n============================================================\n")
    print(f"# Claw-SWE-Bench Lite-80 Summary: Completed Tasks")
    print(f"**Source Log File**: `{log_name}`")
    print(f"**Tasks Evaluated**: `{total_successful} / {total_runs}` successful runs")
    print(f"**Model Evaluated**: `deepseek/deepseek-chat-v3`")
    print("")
    print("## Average Token Metrics")
    print("")
    print("| Evaluation Metric | With Recon (3-Tier) | Without Recon (Baseline) | Savings / Gain |")
    print("| :--- | :--- | :--- | :--- |")
    print(f"| Average Input Tokens | {avg_recon_in:,} | {avg_base_in:,} | **{in_savings} savings** |")
    print(f"| Average Output Tokens | {avg_recon_out:,} | {avg_base_out:,} | **{out_savings} savings** |")
    print(f"| Average Total Tokens | {avg_recon_total:,} | {avg_base_total:,} | **{total_savings} savings** |")
    print("")
    print("## Results Functional Consistency Validation")
    print("")
    print(f"**Test Result Consistency**: `{consistency_rate:.1f}%` ({consistent_count} of {runnable_count} runnable tasks achieved the same test pass/fail outcome)")
    
    if total_successful > runnable_count:
        print(f"- ⚠️ **Unrunnable Tasks Excluded**: {total_successful - runnable_count} tasks were excluded from consistency checks because their test suites could not be run.")
        print("")

    if consistency_rate == 100.0:
        print("- ✅ **Results Validated**: Recon and the baseline achieved identical test execution results in all benchmark instances.")
    else:
        print("- ⚠️ **Results Discrepancy Detected**: Some task outcomes differed between Recon and the baseline:")
        for details in discrepancy_details:
            print(details)

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/summarize_completed.py <log_file_path>")
        sys.exit(1)
        
    log_path = sys.argv[1]
    abs_log_path = os.path.abspath(log_path)
    results = parse_log(abs_log_path)
    print_summary(results, os.path.basename(abs_log_path))

if __name__ == "__main__":
    main()
