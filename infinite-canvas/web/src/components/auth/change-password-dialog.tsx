import { useState } from "react";
import { App, Form, Input, Modal } from "antd";

import { useSessionStore } from "@/stores/portal/use-session-store";


type ChangePasswordFormValues = {
    currentPassword: string;
    newPassword: string;
    confirmPassword: string;
};

export function ChangePasswordDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
    // Keep the antd App context access inside the open state so pages can mount the
    // dialog permanently without requiring the <App> provider when it stays closed.
    if (!open) return null;
    return <ChangePasswordDialogInner onClose={onClose} />;
}

function ChangePasswordDialogInner({ onClose }: { onClose: () => void }) {
    const { message } = App.useApp();
    const changePassword = useSessionStore((state) => state.changePassword);
    const [form] = Form.useForm<ChangePasswordFormValues>();
    const [submitting, setSubmitting] = useState(false);

    const submit = async () => {
        const values = await form.validateFields();
        setSubmitting(true);
        try {
            await changePassword(values.currentPassword, values.newPassword);
            message.success("密码已修改");
            form.resetFields();
            onClose();
        } catch {
            message.error("密码修改失败，请检查当前密码和新密码");
        } finally {
            setSubmitting(false);
        }
    };

    return <Modal title="修改密码" open onCancel={onClose} onOk={() => void submit()} okText="保存" cancelText="取消" confirmLoading={submitting}>
        <Form form={form} layout="vertical" requiredMark={false} className="pt-2">
            <Form.Item name="currentPassword" label="当前密码" rules={[{ required: true, message: "请输入当前密码" }]}>
                <Input.Password autoComplete="current-password" />
            </Form.Item>
            <Form.Item name="newPassword" label="新密码" rules={[{ required: true, min: 12, message: "密码长度至少 12 位" }]}>
                <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Form.Item
                name="confirmPassword"
                label="确认新密码"
                dependencies={["newPassword"]}
                rules={[
                    { required: true, message: "请再次输入新密码" },
                    { validator: (_, value) => !value || value === form.getFieldValue("newPassword") ? Promise.resolve() : Promise.reject(new Error("两次输入的密码不一致")) },
                ]}
            >
                <Input.Password autoComplete="new-password" />
            </Form.Item>
        </Form>
    </Modal>;
}
