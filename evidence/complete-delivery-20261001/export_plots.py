"""Optional matplotlib export of frozen results; never changes accounts or selection."""
import argparse
import csv
import json
from pathlib import Path
from validate_exports import validated
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def export(spot_path, perp_path, assessment_path, output):
    spot, perp, assessment = validated(spot_path, perp_path, assessment_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destinations = [output / name for name in ('candidate-risk-return.svg', 'fixed-capital-equity.svg', 'candidate-summary.csv', 'descriptive-alpha-beta.svg', 'candidate-risk-return.png', 'fixed-capital-equity.png', 'descriptive-alpha-beta.png')]
    if any(p.exists() for p in destinations):
        raise ValueError('choose a fresh export directory; never overwrite evidence')
    summaries = []
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), constrained_layout=True)
    for ax, project, accounts, target, limit in (
        (axes[0], 'Spot', [(name, row) for name, row in spot['results'].items() if row['scenario'] == 'base'], 100, 30),
        (axes[1], 'Perpetual', [(name, rows['base']) for name, rows in perp['results'].items()], 150, 50)):
        for name, row in accounts:
            akey = 'spot/' + name if project == 'Spot' else 'perp/' + name + '/base'
            stats = assessment['candidate_attribution'].get(akey, {})
            passed = row['complete'] and row['audit']['passed']
            summaries.append({'project': project, 'candidate': name, 'complete_audited': passed,
                              'cagr': row['cagr'], 'continuous_proxy_mdd': row['mdd'],
                              'final_cny': row['final_cny'], 'daily_beta_btc_usdt': stats.get('usdt_btc_regression', {}).get('beta_btc'),
                              'native_execution_verified': False})
            if not passed or row['cagr'] is None:
                continue
            x, y = float(row['mdd']) * 100, row['cagr'] * 100
            ax.scatter(x, y, s=50, label=name.replace('-base', ''))
        ax.axhline(target, linestyle='--', color='#777777', label='Original CAGR target')
        ax.axvline(limit, linestyle=':', color='#777777', label='Original MDD boundary')
        ax.set(xlabel='Continuous proxy MDD (%)', ylabel='Cost-net CAGR (%)', title=project + ': registered base scenarios')
        ax.grid(alpha=.2)
        ax.legend(fontsize=8, loc='lower left')
    fig.suptitle('BTC historical account diagnostics — proxy execution; no prospective alpha or native proof', fontsize=11)
    fig.savefig(destinations[0])
    fig.savefig(output / 'candidate-risk-return.png', dpi=160)
    plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, constrained_layout=True)
    plot_rows = list(assessment.get('joint_selected_fixed_capital', assessment['joint_fixed_capital']))
    if 'joint_selected_fixed_capital' in assessment:
        plot_rows += [r for r in assessment['joint_fixed_capital'] if r['spot_initial_cny'] == 5000]
    for index, row in enumerate(plot_rows):
        values = row['daily_equity_cny']
        if row['spot_initial_cny'] not in (0, 5000, 10000):
            continue
        label = '%s Spot %s / %s Perpetual %s CNY' % (row['spot_candidate'], row['spot_initial_cny'], row['perp_candidate'], row['perp_initial_cny'])
        style = '--' if index >= 5 else '-'
        peak, dd = 10000, []
        for v in values:
            peak = max(peak, v)
            dd.append(100 * (v / peak - 1))
        axes[0].plot(values, label=label, linestyle=style)
        axes[1].plot(dd, label=label, linestyle=style)
    axes[0].set(yscale='log', ylabel='Total equity CNY (log scale)', title='Fixed CNY10,000; actual budget accounts; no transfers or rebalancing')
    axes[1].set(ylabel='Daily drawdown (%)', xlabel='UTC days since 2020-01-01')
    axes[0].legend(fontsize=8)
    for ax in axes:
        ax.grid(alpha=.2)
    fig.suptitle('Daily curves only — continuous joint drawdown is not verified', fontsize=11)
    fig.savefig(destinations[1])
    fig.savefig(output / 'fixed-capital-equity.png', dpi=160)
    plt.close(fig)
    with destinations[2].open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    fig, axes = plt.subplots(1, 2, figsize=(13, 6), constrained_layout=True)
    for ax, project, prefix in ((axes[0], 'Spot', 'spot/'), (axes[1], 'Perpetual', 'perp/')):
        for name, stats in assessment['candidate_attribution'].items():
            if not name.startswith(prefix) or not (name.endswith('-base') if prefix == 'spot/' else name.endswith('/base')):
                continue
            r = stats['usdt_btc_regression']
            y = 100 * r['residual_arithmetic_annualized']
            low, high = r['residual_arithmetic_annualized_normal95_descriptive']
            label = name.removeprefix(prefix).removesuffix('-base').removesuffix('/base')
            ax.errorbar(r['beta_btc'], y, yerr=[[max(0, y - 100 * low)], [max(0, 100 * high - y)]],
                        fmt='o', capsize=3, label=label, alpha=.8)
        ax.axhline(0, color='#777777', linestyle=':')
        ax.set(xlabel='Daily OLS beta to BTCUSDT', ylabel='Descriptive arithmetic annual residual (%)', title=project)
        ax.grid(alpha=.2)
        ax.legend(fontsize=8)
    fig.suptitle('Previously studied history: HAC7 normal 95% intervals; no selection adjustment or future alpha proof', fontsize=10)
    fig.savefig(destinations[3])
    fig.savefig(output / 'descriptive-alpha-beta.png', dpi=160)
    plt.close(fig)
    return [str(p) for p in destinations]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spot', required=True)
    parser.add_argument('--perp', required=True)
    parser.add_argument('--assessment', required=True)
    parser.add_argument('--outdir', required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.spot, args.perp, args.assessment, args.outdir)))
