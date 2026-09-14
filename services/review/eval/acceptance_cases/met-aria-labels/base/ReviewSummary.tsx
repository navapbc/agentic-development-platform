export function ReviewSummary({ rows }: { rows: Row[] }) {
  return (
    <ul>
      {rows.map((r) => (
        <li key={r.id}>
          <button onClick={() => review(r.id)}>Review application</button>
        </li>
      ))}
    </ul>
  );
}
