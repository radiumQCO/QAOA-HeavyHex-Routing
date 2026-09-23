import os
import time
import json
import random
import argparse
import numpy as np
import pandas as pd
import scipy.stats as stats
import matplotlib.pyplot as plt

import warnings
warnings.filterwarnings("ignore")

from qiskit import QuantumCircuit
from qiskit.transpiler import CouplingMap, Target, InstructionProperties
from qiskit.circuit.library import RZGate, SXGate, XGate, CXGate
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.quantum_info import Operator

# =====================================================================
# 1. HARDWARE TARGET GENERATION
# =====================================================================
def build_heavy_hex_target(distance=5):
    cmap = CouplingMap.from_heavy_hex(distance)
    num_qubits = cmap.size()
    target = Target(num_qubits=num_qubits)
    target.add_instruction(RZGate(0), {(i,): InstructionProperties() for i in range(num_qubits)})
    target.add_instruction(XGate(), {(i,): InstructionProperties() for i in range(num_qubits)})
    target.add_instruction(SXGate(), {(i,): InstructionProperties() for i in range(num_qubits)})
    cx_props = {(edge[0], edge[1]): InstructionProperties() for edge in cmap.get_edges()}
    target.add_instruction(CXGate(), cx_props)
    return target, cmap

# =====================================================================
# 2. CIRCUIT SUITE GENERATION
# =====================================================================
def generate_interaction_graph(num_qubits, graph_type, seed):
    np.random.seed(seed)
    interactions = []
    if graph_type == "all-to-all":
        interactions = [(i, j) for i in range(num_qubits) for j in range(i + 1, num_qubits)]
    elif graph_type == "dense":
        all_possible = [(i, j) for i in range(num_qubits) for j in range(i + 1, num_qubits)]
        k = int(len(all_possible) * 0.75)
        interactions = [tuple(x) for x in np.random.permutation(all_possible)[:k]]
    elif graph_type == "sparse":
        all_possible = [(i, j) for i in range(num_qubits) for j in range(i + 1, num_qubits)]
        k = int(len(all_possible) * 0.25)
        interactions = [tuple(x) for x in np.random.permutation(all_possible)[:k]]
    elif graph_type == "ring":
        interactions = [(i, i+1) for i in range(num_qubits - 1)]
        interactions.append((0, num_qubits - 1))
    return interactions

def build_qaoa_circuit(interactions, num_qubits, angle=0.5):
    qc = QuantumCircuit(num_qubits)
    for (i, j) in interactions:
        qc.cx(i, j)
        qc.rz(angle, j)
        qc.cx(i, j)
    return qc

# =====================================================================
# 3. PRE-COMPILERS
# =====================================================================
def order_random(interactions, seed):
    np.random.seed(seed)
    shuffled = list(interactions)
    np.random.shuffle(shuffled)
    return shuffled

def order_hardware_distance(interactions, coupling_map):
    return sorted(interactions, key=lambda pair: coupling_map.distance(pair[0], pair[1]))

def order_reverse_distance(interactions, coupling_map):
    return sorted(interactions, key=lambda pair: coupling_map.distance(pair[0], pair[1]), reverse=True)

# =====================================================================
# 4. STATISTICAL UTILITIES (HOLM-BONFERRONI)
# =====================================================================
def holm_bonferroni_correction(p_values):
    """Applies Holm-Bonferroni step-down correction for multiple comparisons."""
    indexed_p = list(enumerate(p_values))
    sorted_p = sorted(indexed_p, key=lambda x: x[1])
    m = len(p_values)
    corrected_p = [0.0] * m
    
    for rank, (idx, p) in enumerate(sorted_p):
        corr = min(1.0, p * (m - rank))
        if rank > 0:
            # Enforce monotonicity
            corr = max(corr, corrected_p[sorted_p[rank-1][0]])
        corrected_p[idx] = corr
    return corrected_p

# =====================================================================
# 5. BENCHMARK ENGINE V2
# =====================================================================
def run_benchmark_v2(mode="quick"):
    if mode == "quick":
        qubit_sizes = [8, 12]
        graph_types = ["all-to-all", "sparse"]
        instances_per_config = 5
        random_baselines_per_graph = 2
        hex_distance = 3 
    elif mode == "standard":
        qubit_sizes = [8, 12, 16]
        graph_types = ["all-to-all", "dense", "sparse", "ring"]
        instances_per_config = 30 # Увеличено для стат. значимости
        random_baselines_per_graph = 3 # Усредняем 3 разных рандома
        hex_distance = 3 
    else: 
        qubit_sizes = [8, 12, 16, 20]
        graph_types = ["all-to-all", "dense", "sparse", "ring"]
        instances_per_config = 50
        random_baselines_per_graph = 5
        hex_distance = 5 
        
    target, coupling_map = build_heavy_hex_target(distance=hex_distance)
    results = []
    
    print(f"Starting V2 {mode.upper()} benchmark...")
    print(f"Exact unitary verification was performed for N <= 10;")
    print("Larger circuits rely on the mathematical commutativity of the QAOA ZZ terms.")
    
    for n in qubit_sizes:
        layout = list(range(n))
        pm = generate_preset_pass_manager(optimization_level=3, target=target, initial_layout=layout)
        
        for g_type in graph_types:
            for instance_idx in range(instances_per_config):
                graph_seed = n * 10000 + instance_idx
                base_interactions = generate_interaction_graph(n, g_type, graph_seed)
                
                # --- Treatment & Control ---
                int_cust = order_hardware_distance(base_interactions, coupling_map)
                int_rev  = order_reverse_distance(base_interactions, coupling_map)
                
                qc_cust = build_qaoa_circuit(int_cust, n)
                qc_rev  = build_qaoa_circuit(int_rev, n)
                
                # Verify exactly once for small N
                if n <= 10:
                    qc_rand_test = build_qaoa_circuit(order_random(base_interactions, graph_seed), n)
                    if not Operator.from_circuit(qc_rand_test).equiv(Operator.from_circuit(qc_cust)):
                        print(f"ERROR: Unitary mismatch at N={n}. Exiting.")
                        return None
                
                # Compile Treatment & Control
                comp_cust = pm.run(qc_cust)
                comp_rev = pm.run(qc_rev)
                
                cx_cust = comp_cust.count_ops().get('cx', 0)
                depth_cust = comp_cust.depth()
                cx_rev = comp_rev.count_ops().get('cx', 0)
                depth_rev = comp_rev.depth()
                
                # --- Multiple Baselines ---
                baseline_cxs = []
                baseline_depths = []
                
                for b_idx in range(random_baselines_per_graph):
                    rand_seed = graph_seed + b_idx * 777
                    int_rand = order_random(base_interactions, rand_seed)
                    qc_rand = build_qaoa_circuit(int_rand, n)
                    comp_rand = pm.run(qc_rand)
                    
                    baseline_cxs.append(comp_rand.count_ops().get('cx', 0))
                    baseline_depths.append(comp_rand.depth())
                
                # Aggregate Baselines (Mean)
                mean_baseline_cx = np.mean(baseline_cxs)
                mean_baseline_depth = np.mean(baseline_depths)
                
                record = {
                    "qubits": n,
                    "graph_type": g_type,
                    "instance_seed": graph_seed,
                    "cx_baseline_mean": mean_baseline_cx,
                    "cx_custom": cx_cust,
                    "cx_reverse": cx_rev,
                    "depth_baseline_mean": mean_baseline_depth,
                    "depth_custom": depth_cust,
                    "depth_reverse": depth_rev,
                    "cx_improvement_pct": ((mean_baseline_cx - cx_cust) / mean_baseline_cx * 100) if mean_baseline_cx > 0 else 0,
                    "depth_improvement_pct": ((mean_baseline_depth - depth_cust) / mean_baseline_depth * 100) if mean_baseline_depth > 0 else 0
                }
                results.append(record)
                
            print(f"Processed: N={n:<2}, Topology={g_type}")

    df = pd.DataFrame(results)
    df.to_csv(f"benchmark_v2_results_{mode}.csv", index=False)
    return df

# =====================================================================
# 6. STATISTICAL ANALYSIS & REPORTING V2
# =====================================================================
def analyze_and_plot_v2(df, mode):
    print("\n" + "="*80)
    print(" V2 EMPIRICAL RESEARCH REPORT (Strict Statistical Significance)")
    print("="*80)
    
    summary_list = []
    raw_p_values = []
    group_keys = []
    
    groups = df.groupby(['qubits', 'graph_type'])
    
    # Pass 1: Calculate raw p-values using Wilcoxon Signed-Rank
    for (n, g_type), group in groups:
        baseline_vals = group['cx_baseline_mean'].values
        custom_vals = group['cx_custom'].values
        
        # Wilcoxon test requires non-zero differences
        diffs = baseline_vals - custom_vals
        if np.all(diffs == 0):
            p_val = 1.0
        else:
            try:
                _, p_val = stats.wilcoxon(baseline_vals, custom_vals, zero_method='zsplit')
            except ValueError:
                p_val = 1.0 # Fallback if all diffs are 0 after rounding
                
        raw_p_values.append(p_val)
        group_keys.append((n, g_type, group))
        
    # Pass 2: Apply Holm-Bonferroni Correction
    corrected_p_values = holm_bonferroni_correction(raw_p_values)
    
    # Pass 3: Calculate Bootstrap CI and build summary
    for idx, (n, g_type, group) in enumerate(group_keys):
        cx_imp = group['cx_improvement_pct'].values
        
        # 95% Bootstrap CI
        if np.all(cx_imp == cx_imp[0]): # If all values identical, CI is 0
            ci_low, ci_high = cx_imp[0], cx_imp[0]
        else:
            res = stats.bootstrap((cx_imp,), np.mean, confidence_level=0.95, method='percentile')
            ci_low, ci_high = res.confidence_interval.low, res.confidence_interval.high
            
        win_rate_cx = np.mean(cx_imp > 0) * 100
        corr_p = corrected_p_values[idx]
        
        summary_list.append({
            "Qubits": n,
            "Topology": g_type,
            "Mean_Imp_%": np.mean(cx_imp),
            "Median_Imp_%": np.median(cx_imp),
            "Std_Dev": np.std(cx_imp),
            "95%_CI_Low": ci_low,
            "95%_CI_High": ci_high,
            "Win_Rate_%": win_rate_cx,
            "Holm_P_Value": corr_p
        })
        
    df_summary = pd.DataFrame(summary_list)
    
    # Print formatted output
    pd.options.display.float_format = '{:.2f}'.format
    # Print p-value in scientific notation
    formatters = {'Holm_P_Value': '{:.2e}'.format}
    print(df_summary.to_string(index=False, formatters=formatters))
    df_summary.to_csv(f"benchmark_v2_summary_{mode}.csv", index=False)
    
    # V2 PLOT
    try:
        plt.style.use('seaborn-v0_8-whitegrid')
    except:
        pass
        
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    
    # Plot 1: CX Improvement vs Qubits
    for g_type in df['graph_type'].unique():
        sub_df = df[df['graph_type'] == g_type]
        means = sub_df.groupby('qubits')['cx_improvement_pct'].mean()
        # Use Standard Error for error bars instead of Std Dev for cleaner plots
        sems = sub_df.groupby('qubits')['cx_improvement_pct'].sem()
        axes[0].errorbar(means.index, means.values, yerr=sems.values, marker='o', label=g_type, capsize=4, linewidth=2)
        
    axes[0].set_title('Mean Transpiled CX Reduction vs. Qubit Count (w/ SEM)')
    axes[0].set_xlabel('Logical Qubit Count')
    axes[0].set_ylabel('CX Reduction relative to Qiskit L3 (%)')
    axes[0].axhline(0, color='black', linestyle='--')
    axes[0].legend()
    
    # Plot 2: Boxplot for largest N
    max_n = df['qubits'].max()
    sub_df_max = df[df['qubits'] == max_n]
    data_to_plot = [
        sub_df_max['cx_reverse'].values,
        sub_df_max['cx_baseline_mean'].values,
        sub_df_max['cx_custom'].values
    ]
    axes[1].boxplot(data_to_plot, tick_labels=['Adversarial (Reverse)', 'Baseline (Random Mean)', 'Treatment (Custom)'])
    axes[1].set_title(f'Transpiled CX Distributions at N={max_n}')
    axes[1].set_ylabel('Transpiled CX Count')
    
    plt.tight_layout()
    plt.savefig(f'benchmark_v2_plots_{mode}.png', dpi=300)
    
    print("\n" + "="*80)
    print(" BENCHMARK CONCLUSIONS (V2)")
    print("="*80)
    print(f"- Exact unitary verification was performed for N <= 10; larger circuits rely on the mathematical commutativity of the QAOA ZZ terms.")
    print("- Evaluation incorporated multiple random orderings per graph, Wilcoxon signed-rank testing, and Holm-Bonferroni correction.")
    print(f"- Benchmark artifacts saved: benchmark_v2_results_{mode}.csv, benchmark_v2_summary_{mode}.csv, benchmark_v2_plots_{mode}.png")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V2 Academic Benchmark for QAOA Routing")
    parser.add_argument("--mode", type=str, choices=["quick", "standard", "full"], default="standard")
    args = parser.parse_args()
    
    df_raw = run_benchmark_v2(mode=args.mode)
    if df_raw is not None:
        analyze_and_plot_v2(df_raw, mode=args.mode)