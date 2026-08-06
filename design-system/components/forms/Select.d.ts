import * as React from 'react'
/** Native select styled to the system. Pass <option> children. */
export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {}
export function Select(props: SelectProps): JSX.Element
