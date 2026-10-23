interface LoadingProps {
  label?: string;
}

export function Loading({ label = "Carregando..." }: LoadingProps) {
  return (
    <div aria-busy="true" aria-live="polite" className="loading">
      <span aria-hidden="true" className="loading__spinner" />
      <span>{label}</span>
    </div>
  );
}
