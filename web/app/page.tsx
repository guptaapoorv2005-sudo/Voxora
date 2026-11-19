import Link from "next/link";
import { ArrowDownRight, ArrowRight, AudioLines, CalendarDays, MessageCircle, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function Home() {
  return <main className="site-shell">
    <header className="site-header wrap">
      <Link href="/" className="brand" aria-label="Voxora home"><span className="brand-mark"><AudioLines size={19} /></span> voxora<span className="brand-dot">.</span></Link>
      <nav aria-label="Main navigation" className="header-nav"><a href="#how-it-works">How it works</a><Link href="/dashboard">For the clinic <ArrowRight size={15} /></Link></nav>
    </header>
    <section className="hero wrap">
      <div className="hero-copy reveal">
        <div className="eyebrow"><span className="eyebrow-line" /> A MORE HUMAN WAY TO CONNECT</div>
        <h1>A little less waiting.<br /><em>A lot more care.</em></h1>
        <p>Meet the voice receptionist for BrightSmile Dental Clinic. Ask a question, find a time, or manage your appointment—simply by talking.</p>
        <div className="hero-actions"><Button asChild size="lg"><Link href="/voice">Talk to the receptionist <ArrowRight size={17} /></Link></Button><a className="text-link" href="#how-it-works">See how it works <ArrowDownRight size={16} /></a></div>
        <div className="hero-note"><span className="note-dot" /> An AI receptionist, here to help you get started.</div>
      </div>
      <div className="hero-art reveal" aria-hidden="true">
        <div className="art-top"><span>BRIGHTSMILE / VOICE RECEPTION</span><span>01 — 03</span></div>
        <div className="orb-wrap"><div className="orb-ring ring-one" /><div className="orb-ring ring-two" /><div className="orb"><AudioLines size={58} strokeWidth={1} /></div></div>
        <div className="art-bottom"><div><span className="art-label">A CONVERSATION THAT CARES</span><p>How can we help<br /><em>you today?</em></p></div><span className="art-arrow">↗</span></div>
      </div>
    </section>
    <section id="how-it-works" className="steps-section"><div className="wrap"><div className="steps-heading"><div><div className="eyebrow"><span className="eyebrow-line" /> SIMPLE BY DESIGN</div><h2>Good care starts<br /><em>with a conversation.</em></h2></div><p>From a quick question to your next visit, we’ll help you find your way.</p></div>
      <div className="steps-grid"><article><span className="step-number">01 / SAY HELLO</span><MessageCircle /><h3>Start talking</h3><p>Allow microphone access and tell the receptionist what you need.</p></article><article><span className="step-number">02 / FIND YOUR ANSWER</span><CalendarDays /><h3>Talk it through</h3><p>Ask about the clinic or explore available appointment times.</p></article><article><span className="step-number">03 / CONFIRM TOGETHER</span><ShieldCheck /><h3>Know what’s next</h3><p>Appointment changes are read back for your confirmation.</p></article></div>
    </div></section>
    <footer className="site-footer wrap"><span>voxora<span className="brand-dot">.</span> <small>for BrightSmile Dental Clinic</small></span><Link href="/dashboard">Clinic owner sign in ↗</Link></footer>
  </main>;
}
