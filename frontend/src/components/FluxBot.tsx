import { useEffect, useRef } from "react";
import "./FluxBot.css";

export function FluxBot({ computing }: { computing: boolean }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const pLRef = useRef<SVGCircleElement>(null);
  const pRRef = useRef<SVGCircleElement>(null);
  const target = useRef({ x: 158.5, y: 106.5 });
  const cur = useRef({ x: 158.5, y: 106.5 });
  const computingRef = useRef(computing);
  const pointerInside = useRef(false);
  computingRef.current = computing;

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      pointerInside.current = true;
      const svg = svgRef.current;
      if (!svg) return;
      const pt = (svg as unknown as { createSVGPoint: () => DOMPoint }).createSVGPoint();
      pt.x = e.clientX; pt.y = e.clientY;
      const ctm = svg.getScreenCTM();
      if (!ctm) return;
      const p = pt.matrixTransform(ctm.inverse());
      target.current.x = p.x; target.current.y = p.y;
    };
    const onLeave = () => { pointerInside.current = false; target.current = { x: 158.5, y: 106.5 }; };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerleave", onLeave);
    const wander = setInterval(() => {
      if (pointerInside.current || computingRef.current) return;
      target.current.x = 151 + Math.random() * 14;
      target.current.y = 118 + Math.random() * 8 - 4;
    }, 1800);
    let raf = 0;
    const tick = () => {
      cur.current.x += (target.current.x - cur.current.x) * 0.16;
      cur.current.y += (target.current.y - cur.current.y) * 0.16;
      const apply = (el: SVGCircleElement | null, cx: number, cy: number) => {
        if (!el) return;
        const dx = cur.current.x - cx, dy = cur.current.y - cy;
        const computing = computingRef.current;
        const factor = computing ? 0.038 : 0.055, maxD = computing ? 3.1 : 5.4;
        const ang = Math.atan2(dy, dx), d = Math.min(maxD, Math.hypot(dx, dy) * factor);
        el.setAttribute("transform", "translate(" + (Math.cos(ang)*d).toFixed(2) + " " + (Math.sin(ang)*d).toFixed(2) + ")");
      };
      apply(pLRef.current, 121, 122); apply(pRRef.current, 168, 122);
      raf = requestAnimationFrame(tick);
    };
    tick();
    const blink = () => {
      if (computingRef.current) { setTimeout(blink, 1800 + Math.random()*3600); return; }
      [pLRef.current, pRRef.current].forEach((el) => el?.animate?.([{transform:"scaleY(1)"},{transform:"scaleY(.12)"},{transform:"scaleY(1)"}], {duration:150, easing:"ease-in-out"} as KeyframeAnimationOptions));
      setTimeout(blink, 1800 + Math.random()*3600);
    };
    setTimeout(blink, 1600);
    return () => { window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerleave", onLeave); clearInterval(wander); cancelAnimationFrame(raf); };
  }, []);

  return (
    <svg ref={svgRef} viewBox="0 0 317 213" role="img" aria-label="Flux bot — eyes follow your cursor" style={{ width: "100%", height: "auto", overflow: "visible", filter: "drop-shadow(0 14px 18px rgba(0,0,0,.06))", display: "block" }}>
      <defs><clipPath id="fluxFaceClip"><rect x={95} y={90} width={100} height={66} rx={30} /></clipPath></defs>
      <g style={{ transformOrigin: "50% 65%", animation: computing ? "computeFloat 1.1s ease-in-out infinite" : "idleFloat 4.2s ease-in-out infinite" }}>
        <g style={{ transformOrigin: "145px 120px", animation: computing ? "computeTilt .55s ease-in-out infinite alternate" : "idleTilt 5.6s ease-in-out infinite" }}>
          <rect x={143} y={26} width={7} height={32} rx={3.5} fill="var(--bot, #129d8e)" />
          <circle cx={146.5} cy={19} r={10.5} fill="var(--bot, #129d8e)" style={{ transformOrigin: "146.5px 19px", animation: computing ? "computePulse .55s ease-in-out infinite alternate" : "idlePulse 2.4s ease-in-out infinite" }} />
          <rect x={49} y={102} width={18} height={45} rx={9} fill="var(--bot, #129d8e)" />
          <rect x={223} y={102} width={18} height={45} rx={9} fill="var(--bot, #129d8e)" />
          <rect x={72} y={57} width={146} height={127} rx={35} fill="var(--bot, #129d8e)" />
          <rect x={95} y={90} width={100} height={66} rx={30} fill="var(--face, #073c3a)" />
          <g clipPath="url(#fluxFaceClip)"><rect x={101} y={118} width={88} height={5} rx={2.5} fill="var(--eye, #54dfce)" style={{ opacity: computing ? 0.75 : 0, transition: "opacity 160ms ease", animation: computing ? "scan 1.15s linear infinite" : undefined }} /></g>
          <circle cx={121} cy={122} r={11} fill="var(--eye, #54dfce)" />
          <circle cx={168} cy={122} r={11} fill="var(--eye, #54dfce)" />
          <circle ref={pLRef} cx={121} cy={122} r={4.1} fill="var(--face, #073c3a)" style={{ transition: "transform 45ms linear", animation: computing ? "thinkingEyes .38s linear infinite alternate" : undefined }} />
          <circle ref={pRRef} cx={168} cy={122} r={4.1} fill="var(--face, #073c3a)" style={{ transition: "transform 45ms linear", animation: computing ? "thinkingEyes .38s linear infinite alternate" : undefined }} />
          {[0,1,2,3,4].map((i) => <rect key={i} x={117+i*9} y={i===2?133:i===1||i===3?137:141} width={5} height={i===2?17:i===1||i===3?13:9} rx={2} fill="var(--eye, #54dfce)" style={{ opacity: computing ? 1 : 0, transformOrigin: "center", transition: "opacity 160ms ease", animation: computing ? "statusBars .8s ease-in-out infinite" : undefined, animationDelay: (i*0.08)+"s" }} />)}
        </g>
      </g>
    </svg>
  );
}
