export default function MetricCard({ label, value, note, tone = "brand" }) {
  // 统一的指标卡片，工作台/作品/用户页都复用它来保持视觉一致。
  return (
    <section className={`metric-card ${tone}`}>
      <div className="metric-dot" />
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        <p>{note}</p>
      </div>
    </section>
  );
}
