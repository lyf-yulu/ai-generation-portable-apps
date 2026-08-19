import { useState } from "react";
import { App, Button, Checkbox, Form, Input, Modal } from "antd";

import { setAdminUserPassword, type AdminUser } from "@/api/admin";


type UserPasswordFormValues = {
    newPassword: string;
    confirmPassword: string;
    mustChangePassword: boolean;
};

const PASSWORD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

function generateRandomPassword(): string {
    const bytes = crypto.getRandomValues(new Uint8Array(18));
    return Array.from(bytes, (byte) => PASSWORD_ALPHABET[byte % PASSWORD_ALPHABET.length]).join("");
}

export function UserPasswordDialog({ user, onClose, onUpdated }: { user: AdminUser; onClose: () => void; onUpdated: (updated: AdminUser) => void }) {
    const { message } = App.useApp();
    const [form] = Form.useForm<UserPasswordFormValues>();
    const [submitting, setSubmitting] = useState(false);

    const generate = () => {
        const generated = generateRandomPassword();
        form.setFieldsValue({ newPassword: generated, confirmPassword: generated });
        message.info("已生成随机密码，请复制后交付给用户");
    };

    const submit = async () => {
        const values = await form.validateFields();
        setSubmitting(true);
        try {
            const updated = await setAdminUserPassword(user.user_id, values.newPassword, values.mustChangePassword);
            message.success(`已为 ${user.display_name} 设置新密码`);
            form.resetFields();
            onUpdated(updated);
            onClose();
        } catch {
            message.error("设置失败，该账号不存在或不可操作");
        } finally {
            setSubmitting(false);
        }
    };

    return <Modal title={`设置密码 · ${user.display_name}`} open onCancel={onClose} onOk={() => void submit()} okText="保存" cancelText="取消" confirmLoading={submitting} destroyOnHidden>
        <Form form={form} layout="vertical" requiredMark={false} initialValues={{ mustChangePassword: false }} className="pt-2">
            <Form.Item name="newPassword" label="新密码" rules={[{ required: true, min: 12, message: "密码长度至少 12 位" }]}>
                <Input.Password autoComplete="new-password" placeholder="至少 12 位" addonAfter={<Button type="text" size="small" onClick={generate}>随机生成</Button>} />
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
            <Form.Item name="mustChangePassword" valuePropName="checked">
                <Checkbox>下次登录需改密</Checkbox>
            </Form.Item>
        </Form>
    </Modal>;
}
