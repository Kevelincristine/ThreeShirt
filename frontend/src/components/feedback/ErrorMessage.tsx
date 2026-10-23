interface ErrorMessageProps {
  message: string;
}

export function ErrorMessage({ message }: ErrorMessageProps) {
  return (
    <p className="error-message" role="alert">
      {message}
    </p>
  );
}
