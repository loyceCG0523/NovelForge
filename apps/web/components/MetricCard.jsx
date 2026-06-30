export default function MetricCard({ label, value, note, tone = "brand" }) {
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
