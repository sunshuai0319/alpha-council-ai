import { ArrowUpRight, Radio, ShieldCheck } from "lucide-react"
import Link from "next/link"

export default function HomePage() {
  return <main className="landing-page">
    <nav className="landing-nav"><Link className="brand brand--landing" href="/"><span className="brand-mark"><Radio size={17} /></span><span><b>alpha council</b><small>AI trading lab</small></span></Link><div><Link className="landing-button" href="/dashboard">Open console <ArrowUpRight size={15} /></Link></div></nav>
    <section className="landing-hero"><div className="landing-kicker"><span className="status-dot" /> virtual futures / bounded autonomy</div><h1>Conviction with<br /><em>guardrails.</em></h1><p>Alpha Council turns market data, independent AI analysis and hard risk limits into an auditable WEEX virtual futures workflow.</p><div className="landing-actions"><Link className="button button--signal" href="/dashboard">Enter the lab <ArrowUpRight size={16} /></Link><span><ShieldCheck size={15} /> no live funds · no custody · simulated execution</span></div></section>
    <section className="landing-grid"><div><span className="eyebrow">01 / observe</span><strong>Fresh exchange context</strong><p>WEEX candles, ticker and account state are captured as time-stamped facts.</p></div><div><span className="eyebrow">02 / debate</span><strong>Three-agent committee</strong><p>Market, quant and macro lenses write structured reasoning before a proposal exists.</p></div><div><span className="eyebrow">03 / constrain</span><strong>Risk has veto power</strong><p>Stale data, leverage, loss and notional limits fail closed before execution.</p></div></section>
  </main>
}
