import type { Page } from "./types";

/**
 * Suggested questions offered when the Ask Flux thread is empty. Every prompt
 * must be answerable through the governed tool surface (cost, changes,
 * anomalies, opportunities, telemetry, inventory, governance, reports) so a
 * suggestion never leads to a refusal.
 */
type PromptCategory = {
  id: string;
  prompts: string[];
};

export const PROMPT_CATALOG: PromptCategory[] = [
  {
    id: "cost",
    prompts: [
      "What changed in amortized cost this month compared to last?",
      "Which subscriptions grew fastest over the last 30 days?",
      "Break down current spend by service and show the top movers.",
      "How does billed cost compare to amortized cost right now?",
      "Which resource groups account for the top 80% of spend?",
      "Show the daily cost trend and call out any inflection points.",
      "What is my current run-rate versus last quarter?",
      "Which regions drive the most spend, and is that mix shifting?",
      "Break down spend by charge category and flag anything unusual.",
    ],
  },
  {
    id: "changes",
    prompts: [
      "What are the largest cost changes in the last 7 days, and why?",
      "Explain the biggest week-over-week increase in detail.",
      "Which resources were created recently and what are they costing?",
      "Show scale-downs or deletions that reduced cost this month.",
      "Did any SKU changes drive a cost increase recently?",
      "Attribute this month's increase to specific resources.",
      "Which single change had the largest impact on amortized cost?",
      "Show cost changes in subscriptions with no owner tag.",
    ],
  },
  {
    id: "anomalies",
    prompts: [
      "Show open cost anomalies ranked by impact.",
      "Explain how Flux calculates a cost anomaly.",
      "Which anomalies from last week are still unresolved?",
      "Is the most recent anomaly a real spike or a data artifact?",
      "Which services produce anomalies most often?",
      "Show anomalies that line up with a resource change.",
      "What is the total unexplained variance across open anomalies?",
      "Which anomalies affect production subscriptions?",
    ],
  },
  {
    id: "opportunities",
    prompts: [
      "Show the highest-value optimization opportunities and their confidence.",
      "What can I save this quarter without taking downtime?",
      "Which opportunities have the best savings-to-effort ratio?",
      "How much are idle and orphaned resources costing me?",
      "Show low-risk opportunities I could action today.",
      "What is my total identified savings, and how much is verified?",
      "Where am I paying on-demand for steady-state workloads?",
      "Compare reserved instance and savings plan coverage gaps.",
    ],
  },
  {
    id: "rightsizing",
    prompts: [
      "Which VMs are the strongest right-sizing candidates?",
      "Where are telemetry coverage gaps limiting right-sizing decisions?",
      "Show oversized VMs with at least 30 days of observed data.",
      "Which workloads are memory-constrained but CPU-idle?",
      "What would I save by actioning every high-confidence right-sizing?",
      "Show VMs under 5% average CPU that are still powered on.",
      "Which right-sizing recommendations need a special workload review?",
      "How complete is utilization telemetry, by subscription?",
      "Review the right-sizing purchase plan for placements that contradict telemetry.",
      "Which unassigned VMs in the purchase plan fit an existing commitment bucket?",
      "Summarize the purchase plan: buckets, quantities, and decision progress.",
    ],
  },
  {
    id: "inventory",
    prompts: [
      "How many VMs do I have, by region and power state?",
      "Find resources that are stopped but not deallocated.",
      "Show resources missing an owner or application tag.",
      "What is deployed in regions I do not normally use?",
      "List unattached public IPs, disks, and load balancers.",
      "Which resources were created in the last 7 days?",
      "Show my largest resources by cost with their current SKU.",
      "Find non-production resources running on premium tiers.",
    ],
  },
  {
    id: "governance",
    prompts: [
      "What is my tag compliance rate, and where are the gaps?",
      "Which subscriptions fail policy most often?",
      "How much spend is unallocated to a cost center?",
      "Show governance posture trends over the last month.",
      "Which policies have the most non-compliant resources?",
      "What share of spend can I attribute to a named owner?",
      "Which teams are tracking over their budget target?",
      "Show untagged spend broken down by subscription.",
    ],
  },
  {
    id: "reporting",
    prompts: [
      "What reports are available, and what does each one cover?",
      "How fresh is my cost data right now?",
      "Explain how Flux calculates amortized cost.",
      "Which data sources are stale, and what does that affect?",
      "How does Flux decide right-sizing confidence?",
      "What is FOCUS, and how does Flux use it?",
      "Summarize this month's cost story for an executive update.",
      "What feeds the opportunities page, and how often does it refresh?",
      "What will we spend this fiscal year, and how does it compare to budget?",
      "What assumptions are behind the fiscal-year forecast?",
    ],
  },
];

/**
 * Page whose subject matter maps onto a catalog category. Keyed loosely by
 * page id rather than the Page union so entries can be staged for pages that
 * are still in flight; an id that does not exist yet simply never matches.
 */
const PAGE_CATEGORY: Record<string, string> = {
  overview: "cost",
  changes: "changes",
  "cost-anomalies": "anomalies",
  opportunities: "opportunities",
  rightsizing: "rightsizing",
  inventory: "inventory",
  integrations: "governance",
  reports: "reporting",
};

function shuffled<T>(items: T[]): T[] {
  const copy = [...items];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(Math.random() * (index + 1));
    [copy[index], copy[swap]] = [copy[swap], copy[index]];
  }
  return copy;
}

/**
 * One prompt from each of `count` distinct categories, so a draw always spans
 * different subject areas. When the caller is on a page that maps to a
 * category, that category leads; the rest stay random.
 */
export function pickSuggestions(page?: Page, count = 4): string[] {
  const pools = shuffled(PROMPT_CATALOG);
  const preferred = page ? PAGE_CATEGORY[page] : undefined;
  if (preferred) {
    const index = pools.findIndex((pool) => pool.id === preferred);
    if (index > 0) pools.unshift(...pools.splice(index, 1));
  }
  return pools
    .slice(0, count)
    .map((pool) => pool.prompts[Math.floor(Math.random() * pool.prompts.length)]);
}
