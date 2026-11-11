"use client";
import { useEffect, useRef, useState, type InputHTMLAttributes } from "react";
/** Keep an in-progress decimal intact while the numeric model updates live. */
export default function MetricInput({
  value,
  onValueChange,
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange"> & {
  value: number;
  onValueChange: (value: number) => void;
}) {
  const [text, setText] = useState(String(value)),
    focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setText(String(value));
  }, [value]);
  return (
    <input
      {...props}
      type="number"
      value={text}
      onFocus={(event) => {
        focused.current = true;
        props.onFocus?.(event);
      }}
      onBlur={(event) => {
        focused.current = false;
        setText(String(value));
        props.onBlur?.(event);
      }}
      onChange={(event) => {
        const raw = event.target.value;
        setText(raw);
        if (raw !== "" && Number.isFinite(Number(raw)))
          onValueChange(Number(raw));
      }}
    />
  );
}
