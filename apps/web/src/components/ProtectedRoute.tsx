import { useEffect } from "react";
import { Navigate, Outlet } from "react-router-dom";
import { useAuthStore } from "../stores/auth";

export default function ProtectedRoute() {
  const { token, isLoading, checkAuth } = useAuthStore();

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-manor-700 border-t-transparent" />
      </div>
    );
  }

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  return <Outlet />;
}
