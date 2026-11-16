// 画面の切り替え（URL の path だけを見る小さな仕組み）。画面が増えたら react-router などを検討する
import { useEffect, useState } from "react";

const EVENT = "yanua:navigate";

export function navigate(path: string): void {
  if (window.location.pathname + window.location.search !== path) {
    window.history.pushState(null, "", path);
    window.dispatchEvent(new Event(EVENT));
  }
}

export function usePath(): string {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPath(window.location.pathname);
    window.addEventListener("popstate", update);
    window.addEventListener(EVENT, update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener(EVENT, update);
    };
  }, []);
  return path;
}
